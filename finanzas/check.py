"""Integrity checks over the whole ledger, not just one file."""

import json
from collections import defaultdict
from datetime import date

from finanzas.clean import has_long_digits


def walk_chain(conn, account: str) -> tuple[list, list]:
    """Order an account's transactions by following the running balance: the next
    movement is the one whose (balance - amount) equals the current balance.

    Where no row continues the chain there is a break. A missing period (gap) and a
    double-counted row (duplicate) both show up this way, so this is the backstop for
    the dedupe key too: if a bank ever rewrote a past balance and a re-exported row got
    inserted twice, the extra amount cannot fit the chain.

    Returns (rows in chain order, breaks). Each break is (last row before, first row
    after, unexplained cents)."""
    rows = conn.execute(
        "SELECT * FROM transactions WHERE account = ? ORDER BY date_booked, id", (account,)
    ).fetchall()
    by_prev = defaultdict(list)  # balance before the movement -> rows, oldest first
    for r in rows:
        by_prev[r["balance_cents"] - r["amount_cents"]].append(r)

    used, ordered, breaks = set(), [], []
    scan = 0  # next unused row in date order, used to restart after a break
    while len(ordered) < len(rows):
        nxt = None
        if ordered:
            # Earliest booking wins when several rows fit (balances can repeat).
            nxt = next((r for r in by_prev[ordered[-1]["balance_cents"]] if r["id"] not in used), None)
        if nxt is None:
            while rows[scan]["id"] in used:
                scan += 1
            nxt = rows[scan]
            if ordered:
                prev = ordered[-1]
                breaks.append((prev, nxt, nxt["balance_cents"] - nxt["amount_cents"] - prev["balance_cents"]))
        used.add(nxt["id"])
        ordered.append(nxt)
    return ordered, breaks


# Two transfers are the same movement if the amounts mirror each other and the legs
# land within a few days: same-day is the norm, but a value date can differ by one
# business day on each side.
PAIR_WINDOW_DAYS = 3
# Below this, mirrored amounts across accounts are mostly coincidence (two 5 EUR
# Bizums on the same day), and every hit would be noise.
PAIR_MIN_CENTS = 10_000


def _pair_legs(rows_a: list, rows_b: list) -> tuple[list, list, list]:
    """Greedy one-to-one matching of mirrored amounts within the date window.
    Returns (pairs, unmatched from a, unmatched from b)."""
    used, pairs, left = set(), [], []
    for a in rows_a:
        mate = next((b for b in rows_b
                     if b["id"] not in used
                     and b["amount_cents"] == -a["amount_cents"]
                     and abs((date.fromisoformat(b["date"]) - date.fromisoformat(a["date"])).days)
                     <= PAIR_WINDOW_DAYS), None)
        if mate is None:
            left.append(a)
        else:
            used.add(mate["id"])
            pairs.append((a, mate))
    return pairs, left, [b for b in rows_b if b["id"] not in used]


def internal_transfers(conn) -> dict:
    """Both legs of a transfer between the owner's own accounts must be present, or the
    ledger is telling a lie in one direction: an unpaired outgoing leg means money
    left and never arrived (so it was not internal, or an export is missing), and an
    unpaired incoming leg means income counted twice as spending's mirror image.

    Also looks the other way round: a mirrored pair where the two legs are NOT both
    internal is a transfer the rules missed, which inflates both income and spend."""
    rows = conn.execute("SELECT id, account, date, amount_cents, description, category "
                        "FROM transactions ORDER BY date, id").fetchall()
    internal = [r for r in rows if r["category"] == "internos-cuentas"]
    out = [r for r in internal if r["amount_cents"] < 0]
    inc = [r for r in internal if r["amount_cents"] > 0]
    paired, unpaired_out, unpaired_in = _pair_legs(out, inc)

    # Candidates for a missed transfer: big mirrored amounts where neither leg is
    # already accounted for as internal.
    rest = [r for r in rows if r["category"] != "internos-cuentas"
            and abs(r["amount_cents"]) >= PAIR_MIN_CENTS]
    suspects = [
        (a, b) for a, b in _pair_legs([r for r in rest if r["amount_cents"] < 0],
                                      [r for r in rest if r["amount_cents"] > 0])[0]
        if a["account"] != b["account"]
    ]
    return {"paired": paired, "unpaired_out": unpaired_out, "unpaired_in": unpaired_in,
            "suspects": suspects}


def unmasked_digit_runs(conn) -> list[str]:
    """Constraint 6, verified against what actually got persisted."""
    found = []
    for table, col in [("transactions", "description"), ("raw_rows", "data"),
                       ("imports", "filename")]:
        for (value,) in conn.execute(f"SELECT {col} FROM {table}"):
            if has_long_digits(value if col != "data" else json.dumps(json.loads(value))):
                found.append(f"{table}.{col}")
    return found
