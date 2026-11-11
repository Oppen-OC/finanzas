"""The numbers, as pure functions over the ledger.

Every function here takes a connection and returns plain dicts or lists: no
printing, no formatting, no euro strings. The CLI formats, and the web UI in
milestone 4 will call exactly the same functions, or the two will drift apart
and the dashboard will quietly disagree with the terminal.

Two conventions run through the whole module:

- Spend is a positive magnitude (the negation of the stored amount), so a
  category that was refunded more than it was spent shows negative spend. Per
  D13 a refund lands in the category it refunds, so a category total is a net
  cost and is allowed to be negative. Nothing here filters by sign.
- `internal` categories are excluded from income and spend alike (D14). Both
  legs of a movement between the owner's own accounts are their money; counting
  them would invent income and spending at the same time.
"""

import statistics
from datetime import date, timedelta

from finanzas import goals as goals_mod
from finanzas.categories import Category, load as load_categories

# A metric resting on fewer complete months than this is reported with
# low_confidence: true rather than as a confident number. Three is the
# smallest window where a median means anything at all.
MIN_BASELINE_MONTHS = 3

# The burn rate uses a much longer window than the per-category baselines.
# Measured over 11 real months: a 3 month median read 2,108/month and a 12 month
# one 1,168, because 9 of those 11 months sat between 919 and 1,394 and the
# 3 month window happened to hold both outliers (a motorbike and a heavy June).
# A 6 month median still cannot absorb a 4,361 month. The cost of the long
# window is that it is slow to notice a real change in the cost base; the
# monthly table the report prints shows that directly, so the headline number
# does not have to chase it.
BURN_WINDOW_MONTHS = 12

# Recurring detection. A series needs this many charges before it is a pattern
# rather than a coincidence.
MIN_OCCURRENCES = 3
# How far a charge may sit from the series median and still belong to it. A
# subscription's price moves with VAT and plan changes; a top-up does not.
AMOUNT_TOLERANCE = 0.10
# Card charges drift across weekends and month lengths, so the monthly window is
# wider than 30 +/- 2.
CADENCE_WINDOWS = {"monthly": (25, 35, 12), "annual": (350, 380, 1)}
# A series whose last charge is older than this many cadences is treated as
# cancelled and left out of fixed costs.
STALE_CADENCES = 1.5


def _months_between(start: str, end: str) -> list[str]:
    """Every YYYY-MM from start to end inclusive."""
    out, y, m = [], int(start[:4]), int(start[5:7])
    while f"{y:04d}-{m:02d}" <= end:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def month_end(month: str) -> date:
    y, m = int(month[:4]), int(month[5:7])
    return date(y + (m == 12), 1 if m == 12 else m + 1, 1) - timedelta(days=1)


def previous_months(month: str, n: int) -> list[str]:
    """The n months immediately before `month`, oldest first."""
    y, m, out = int(month[:4]), int(month[5:7]), []
    for _ in range(n):
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        out.append(f"{y:04d}-{m:02d}")
    return list(reversed(out))


def coverage(conn) -> tuple[str, str] | None:
    """The window the ledger actually covers for *every* account: the latest of
    the per-account first dates and the earliest of the per-account last ones.

    Deliberately pessimistic. A month where one account's export is missing is
    not a month you can report on, and averaging it into a baseline would
    quietly understate spending for as long as the baseline lives."""
    rows = conn.execute(
        "SELECT account, MIN(date) lo, MAX(date) hi FROM transactions GROUP BY account"
    ).fetchall()
    if not rows:
        return None
    return max(r["lo"] for r in rows), min(r["hi"] for r in rows)


def complete_months(conn) -> list[str]:
    """Months the ledger covers end to end. June 23rd to September 20th means
    July and August: the partial months at both edges are the ones that would
    read as a 60% drop in spending."""
    window = coverage(conn)
    if window is None:
        return []
    lo, hi = window
    return [m for m in _months_between(lo[:7], hi[:7])
            if m + "-01" >= lo and month_end(m).isoformat() <= hi]


def _baseline_months(conn, month: str, n: int = MIN_BASELINE_MONTHS) -> list[str]:
    """The last n complete months before `month`. Incomplete months are skipped
    rather than filled: three real months beat five months two of which are
    holes."""
    return [m for m in complete_months(conn) if m < month][-n:]


def _rows_in(conn, month: str) -> list:
    return conn.execute(
        "SELECT * FROM transactions WHERE substr(date, 1, 7) = ? ORDER BY date, id",
        (month,)).fetchall()


# What the category filter means when the user asks for the rows no rule matched.
# A sentinel rather than a magic empty string: category ids are slugs and one of
# them could plausibly be "none".
UNCATEGORIZED = "__uncategorized__"


def find_transactions(conn, account: str | None = None, month: str | None = None,
                      search: str | None = None, category: str | None = None,
                      limit: int | None = None) -> list:
    """Filtered transactions, newest first. The `list` command and the web page
    both come through here: two implementations of "month plus category plus
    search" drift, and the one nobody is looking at is the one that is wrong.

    A parent category includes its children, because asking for `salidas` and
    getting nothing while `salidas-cafes` has twenty rows is not a useful answer.
    Filters compose with AND; an unset filter adds no clause at all."""
    sql, params = "SELECT * FROM transactions WHERE 1=1", []
    if account:
        sql += " AND account = ?"
        params.append(account)
    if month:
        sql += " AND substr(date, 1, 7) = ?"
        params.append(month)
    if search:
        sql += " AND description_norm LIKE ?"
        params.append(f"%{search.lower()}%")
    if category == UNCATEGORIZED:
        sql += " AND category IS NULL"
    elif category:
        sql += " AND (category = ? OR category LIKE ? || '-%')"
        params += [category, category]
    sql += " ORDER BY date DESC, id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def _split(rows, categories: dict[str, Category]) -> dict:
    """Income, spend and the per-category spend breakdown for a set of rows.
    Uncategorized counts as spend: dropping it would make spending fall every
    time a rule is missing, which is the one direction a budget must not lie."""
    income, spend, by_category = 0, 0, {}
    for r in rows:
        cid = r["category"]
        ctype = categories[cid].type if cid else "expense"
        if ctype == "internal":
            continue
        if ctype == "income":
            income += r["amount_cents"]
        else:
            spend += -r["amount_cents"]
            by_category[cid] = by_category.get(cid, 0) + -r["amount_cents"]
    return {"income_cents": income, "spend_cents": spend, "by_category": by_category}


def _category_spend(conn, month: str, categories: dict[str, Category]) -> dict:
    return _split(_rows_in(conn, month), categories)["by_category"]


def monthly_summary(conn, month: str, categories: dict[str, Category] | None = None) -> dict:
    """Income, spend, net and the category breakdown for one month, each
    category set against its median over the last complete months.

    The category totals sum to spend_cents by construction: they are the same
    pass over the same rows, not a second query that can disagree."""
    categories = categories or load_categories()
    rows = _rows_in(conn, month)
    base = _split(rows, categories)
    baseline = _baseline_months(conn, month)
    history = {m: _category_spend(conn, m, categories) for m in baseline}

    lines = []
    for cid, amount in base["by_category"].items():
        past = [history[m].get(cid, 0) for m in baseline]
        median = round(statistics.median(past)) if past else None
        # A brand new merchant has a zero median, and "infinitely over budget"
        # is not a useful thing to print. No baseline means no deviation.
        deviation = (100 * (amount - median) / abs(median)
                     if median else None)
        lines.append({
            "category": cid,
            "name": categories[cid].name if cid else "Sin categorizar",
            "parent": categories[cid].parent if cid else None,
            "amount_cents": amount,
            "median_cents": median,
            "deviation_pct": deviation,
        })
    lines.sort(key=lambda line: -line["amount_cents"])

    return {
        "month": month,
        "complete": month in complete_months(conn),
        "n_transactions": len(rows),
        "income_cents": base["income_cents"],
        "spend_cents": base["spend_cents"],
        "net_cents": base["income_cents"] - base["spend_cents"],
        "by_category": lines,
        "baseline_months": baseline,
        "low_confidence": len(baseline) < MIN_BASELINE_MONTHS,
    }


def find_recurring(conn, categories: dict[str, Category] | None = None) -> list[dict]:
    """Charges that repeat on a cadence: subscriptions, the gym, insurance.

    Two things make this harder than grouping by merchant and averaging.

    First, a merchant's off-cycle charges sit in the same group as the
    subscription. Anthropic bills 18 EUR on the 23rd of every month, and once
    charged 21.78 EUR on the 1st for going over. Measuring the gaps over all
    four charges gives 8, 22 and 31 days and a median of 22, so the most
    regular subscription in the ledger looks irregular. So the amount cluster
    is found first (charges within AMOUNT_TOLERANCE of the group median) and
    the cadence is measured over the cluster alone: 30 and 31 days, monthly.

    Second, a supermarket visited every few days has a perfectly stable small
    amount and dozens of occurrences. It is rejected on cadence, which is why
    the gap test has to be a real test and not a sanity check: more than half
    the gaps must land in the cadence window. That tolerates one skipped month
    in a long series while still rejecting a merchant visited whenever.

    Outflows in expense categories only. A recurring income (the payroll) is
    worth knowing but it is not a cost, and everything downstream of this
    function is about costs. The category test is not redundant with the sign
    test: a withholding on a promotion is a negative amount sitting in an
    income category (D13), and without it the ledger reports a 121 EUR/year
    "subscription" that is really money arriving, just less of it."""
    categories = categories or load_categories()
    window = coverage(conn)
    if window is None:
        return []
    # "Now" is the end of the ledger, not the wall clock: whether a
    # subscription is still live is a fact about the data, and a report run
    # three weeks later should not silently change its answer.
    as_of = date.fromisoformat(window[1])

    rows = conn.execute(
        "SELECT description_norm, date, amount_cents, category, account "
        "FROM transactions WHERE amount_cents < 0 ORDER BY description_norm, date"
    ).fetchall()
    groups: dict[str, list] = {}
    for r in rows:
        if r["category"] and categories[r["category"]].type != "expense":
            continue
        groups.setdefault(r["description_norm"], []).append(r)

    found = []
    for desc, members in groups.items():
        if len(members) < MIN_OCCURRENCES:
            continue
        median_all = statistics.median(abs(r["amount_cents"]) for r in members)
        cluster = [r for r in members
                   if abs(abs(r["amount_cents"]) - median_all) <= AMOUNT_TOLERANCE * median_all]
        if len(cluster) < MIN_OCCURRENCES:
            continue

        amount = round(statistics.median(abs(r["amount_cents"]) for r in cluster))
        dates = [date.fromisoformat(r["date"]) for r in cluster]
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        cadence = next(
            (name for name, (lo, hi, _) in CADENCE_WINDOWS.items()
             if sum(lo <= g <= hi for g in gaps) * 2 > len(gaps)), None)
        if cadence is None:
            continue

        median_gap = statistics.median(gaps)
        days_since = (as_of - dates[-1]).days
        found.append({
            "description_norm": desc,
            "category": cluster[-1]["category"],
            "account": cluster[-1]["account"],
            "cadence": cadence,
            "occurrences": len(cluster),
            "skipped": len(members) - len(cluster),
            "amount_cents": amount,
            "annual_cents": amount * CADENCE_WINDOWS[cadence][2],
            "median_gap_days": median_gap,
            "first_seen": dates[0].isoformat(),
            "last_seen": dates[-1].isoformat(),
            "days_since_last": days_since,
            # Overdue by half a cycle reads as cancelled. Counting a gym you
            # left as a fixed cost overstates every forward-looking number.
            "active": days_since <= STALE_CADENCES * median_gap,
        })
    return sorted(found, key=lambda f: -f["annual_cents"])


def fixed_vs_variable(conn, month: str, categories: dict[str, Category] | None = None) -> dict:
    """Split a month's spending into what recurs and what does not.

    Fixed means either a category declared fixed in categories.yaml, or a
    merchant find_recurring still considers live. The split is done per
    transaction with one test, so the two halves add up to the month's spend
    exactly and cannot both claim the same charge."""
    categories = categories or load_categories()
    recurring = {r["description_norm"] for r in find_recurring(conn, categories) if r["active"]}
    fixed, variable, items = 0, 0, {}
    for r in _rows_in(conn, month):
        cid = r["category"]
        if cid and categories[cid].type != "expense":
            continue
        amount = -r["amount_cents"]
        if (cid and categories[cid].fixed) or r["description_norm"] in recurring:
            fixed += amount
            items[r["description_norm"]] = items.get(r["description_norm"], 0) + amount
        else:
            variable += amount
    return {"month": month, "fixed_cents": fixed, "variable_cents": variable,
            "total_cents": fixed + variable,
            "fixed_items": sorted(items.items(), key=lambda kv: -kv[1])}


def _in_any(cid: str | None, roots) -> bool:
    """Whether a category is one of `roots` or a child of one (D23's rule)."""
    return cid is not None and any(cid == r or cid.startswith(r + "-") for r in roots)


def burn_rate(conn, categories: dict[str, Category] | None = None,
              exclude: frozenset[str] = frozenset()) -> dict:
    """Trailing median of monthly spend over complete months.

    Median, not mean: one motorbike should not become the monthly cost of
    living. With only two complete months the median *is* the mean, which is
    what low_confidence is warning about.

    The window is BURN_WINDOW_MONTHS rather than the 3 used for per-category
    baselines: a category's median moves on amounts small enough that recency
    matters more than stability, while the burn rate feeds the runway and has
    to survive one unusual purchase.

    `exclude` drops the named categories, children included, from every month
    before the median is taken. The projection uses it for an event that
    `replaces` a category (D31): the median of (spend minus housing) is not the
    median of spend minus the median of housing, because medians do not
    subtract, and only the first is the burn of a life without that cost."""
    categories = categories or load_categories()
    months = complete_months(conn)[-BURN_WINDOW_MONTHS:]
    samples = []
    for m in months:
        s = _split(_rows_in(conn, m), categories)
        dropped = sum(v for c, v in s["by_category"].items() if _in_any(c, exclude))
        samples.append((m, s["spend_cents"] - dropped))
    return {
        "months": months,
        "samples": samples,
        "median_cents": round(statistics.median(s for _, s in samples)) if samples else None,
        "low_confidence": len(months) < MIN_BASELINE_MONTHS,
    }


def liquid_balance(conn) -> dict:
    """Sum of the latest balance on each account (O2, resolved 2026-09-20).

    The chain order, not the date order: `check` follows the running balance to
    establish which movement is genuinely last, and `stats` reports that same
    figure. Two commands disagreeing about the balance would be worse than
    either being wrong."""
    from finanzas.check import walk_chain
    per_account = {}
    for (account,) in conn.execute("SELECT DISTINCT account FROM transactions ORDER BY 1"):
        ordered, _ = walk_chain(conn, account)
        per_account[account] = ordered[-1]["balance_cents"] if ordered else 0
    return {"per_account": per_account, "total_cents": sum(per_account.values())}


def runway(conn, liquid_cents: int | None = None,
           categories: dict[str, Category] | None = None,
           goals: goals_mod.Goals | None = None) -> dict:
    """How many months the liquid balance covers at the current burn rate.

    Three sources, in order: an explicit liquid_cents (the `--liquid` flag),
    then `settings.liquid_eur` in goals.yaml, then the sum of account balances.
    The middle one is what milestone 5.1 added, and it is the one that matters:
    a figure you had to remember to pass on every invocation was an override in
    name only, because the dashboard never passed it."""
    burn = burn_rate(conn, categories)
    balance = liquid_balance(conn)
    goals = goals if goals is not None else goals_mod.load()
    if liquid_cents is not None:
        liquid, source = liquid_cents, "override"
    elif goals.liquid_cents is not None:
        liquid, source = goals.liquid_cents, "goals.yaml"
    else:
        liquid, source = balance["total_cents"], "accounts"
    median = burn["median_cents"]
    return {
        "liquid_cents": liquid,
        "liquid_source": source,
        "per_account": balance["per_account"],
        "burn_cents": median,
        # A month where refunds outweighed spending divides to nonsense.
        "months": liquid / median if median and median > 0 else None,
        "low_confidence": burn["low_confidence"],
    }


def savings_rate(conn, month: str, categories: dict[str, Category] | None = None) -> dict:
    """(income - spend) / income, for the month and for the trailing three.

    Returns None rather than raising when a month had no income at all, which
    is not an edge case for a personal ledger."""
    categories = categories or load_categories()
    window = [m for m in _baseline_months(conn, month, MIN_BASELINE_MONTHS - 1)] + [month]
    totals = {m: _split(_rows_in(conn, m), categories) for m in window}
    this = totals[month]
    rolling_income = sum(t["income_cents"] for t in totals.values())
    rolling_spend = sum(t["spend_cents"] for t in totals.values())
    return {
        "month": month,
        "income_cents": this["income_cents"],
        "spend_cents": this["spend_cents"],
        "rate": ((this["income_cents"] - this["spend_cents"]) / this["income_cents"]
                 if this["income_cents"] else None),
        "rolling_months": window,
        "rolling_rate": ((rolling_income - rolling_spend) / rolling_income
                         if rolling_income else None),
        "low_confidence": len(window) < MIN_BASELINE_MONTHS
                          or month not in complete_months(conn),
    }


def discretionary_share(conn, month: str, categories: dict[str, Category] | None = None) -> dict:
    """What share of the month's spending was optional, by the discretionary
    flag in categories.yaml (salidas and ocio today)."""
    categories = categories or load_categories()
    base = _split(_rows_in(conn, month), categories)
    spent = sum(amount for cid, amount in base["by_category"].items()
                if cid and categories[cid].discretionary)
    return {
        "month": month,
        "discretionary_cents": spent,
        "spend_cents": base["spend_cents"],
        "share": spent / base["spend_cents"] if base["spend_cents"] else None,
    }


def month_to_date(conn, month: str, goals: goals_mod.Goals | None = None,
                  categories: dict[str, Category] | None = None) -> dict:
    """Spending so far this month against the limits in goals.yaml, and where the
    month lands if the rest of it looks like the part that has happened.

    "So far" is measured against the end of the ledger, not the wall clock: the
    same "now" find_recurring uses, and for the same reason. If the last export
    covers the 10th and today is the 20th, those ten days are missing data, not
    ten days of restraint, and scaling by them would report every category as
    comfortably under control on the strength of transactions nobody has
    imported yet.

    A category flagged `fixed` is not projected. Rent, the gym and the phone
    bill land whole on one day, so a charge on the 2nd means the month's cost is
    already known, not that the month is heading for fifteen times it. The cost
    of that choice is the opposite error, a fixed charge that has not landed yet
    showing as zero; that one is visible in the same row, since spent and limit
    sit next to each other, rather than buried in a projection.

    A limit on a parent category covers its children (D23). A limit on `salidas`
    that ignored `salidas-cafes` would be lying about what it measures."""
    categories = categories or load_categories()
    goals = goals if goals is not None else goals_mod.load(categories=categories)

    first = date(int(month[:4]), int(month[5:7]), 1)
    last = month_end(month)
    window = coverage(conn)
    as_of = min(date.fromisoformat(window[1]), last) if window else last
    # Clamped at zero: a month the ledger has not reached at all has no elapsed
    # days, and dividing by them is the one way this function can raise.
    days_elapsed = max(0, (as_of - first).days + 1) if as_of >= first else 0
    days_in_month = last.day

    spent = _split(_rows_in(conn, month), categories)["by_category"]
    lines = []
    for cid, limit in goals.at(month).items():
        amount = sum(v for c, v in spent.items()
                     if c and (c == cid or c.startswith(cid + "-")))
        fixed = categories[cid].fixed
        scaled = not fixed and 0 < days_elapsed < days_in_month
        projected = round(amount * days_in_month / days_elapsed) if scaled else amount
        lines.append({
            "category": cid,
            "name": categories[cid].name,
            "fixed": fixed,
            "limit_cents": limit.monthly_cents,
            "since": limit.since,
            "spent_cents": amount,
            "remaining_cents": limit.monthly_cents - amount,
            "projected_cents": projected,
            "projected": scaled,
            "share": amount / limit.monthly_cents,
            "projected_share": projected / limit.monthly_cents,
            "over": amount > limit.monthly_cents,
            "over_projected": projected > limit.monthly_cents,
        })
    # Worst offenders first: the point of the table is what to stop doing.
    lines.sort(key=lambda line: -line["projected_share"])

    return {
        "month": month,
        "as_of": as_of.isoformat(),
        "days_elapsed": days_elapsed,
        "days_in_month": days_in_month,
        "complete": month in complete_months(conn),
        "lines": lines,
        "limit_cents": sum(line["limit_cents"] for line in lines),
        "spent_cents": sum(line["spent_cents"] for line in lines),
        "projected_cents": sum(line["projected_cents"] for line in lines),
        "n_over": sum(line["over_projected"] for line in lines),
    }


# The annual matrix colours each cell by how far it sits from its own row's
# median. The denominator has a floor so that a 6 EUR wobble on a category whose
# median is 3 EUR is not painted like a motorbike; above the floor it is a plain
# ratio to the median. Zero months count toward the median, so an annual or
# one-off charge has a median of 0 and its month reads as the spike it is.
# 50 EUR, the floor D29 uses for discretionary spend: tried at 20 first, and 22
# EUR of uncategorized card payments came out the same colour as a 400 EUR course.
DEVIATION_FLOOR_CENTS = 5000
ANNUAL_WINDOW_MONTHS = 12


def _matrix_row(cid, name, parent, values: list[int], months: list[str]) -> dict:
    median = round(statistics.median(values)) if values else 0
    scale = max(abs(median), DEVIATION_FLOOR_CENTS)
    return {
        "id": cid,
        "name": name,
        "parent": parent,
        "cells": [{"month": m, "cents": v, "deviation": (v - median) / scale}
                  for m, v in zip(months, values)],
        "total_cents": sum(values),
        "median_cents": median,
        # Nonzero, not positive: a month where a refund outweighed the spend is
        # still a month the category moved money (D13).
        "months_with_spend": sum(v != 0 for v in values),
        "children": [],
    }


def annual_matrix(conn, end_month: str | None = None,
                  categories: dict[str, Category] | None = None) -> dict:
    """Category x month spend over the last twelve complete months (D30).

    Complete months only (D16): a partial month in a matrix is a column of
    cells that all read as unusually cheap. An end_month that is not a complete
    month falls back to the latest one, the same rule the dashboard applies to
    an unknown ?month (D22).

    Each month's figures come from _split over that month's rows, the pass
    monthly_summary makes, so the matrix and the dashboard cannot disagree about
    a month. The footer is _split's spend_cents rather than a sum of the rows,
    so "the rows add up to the month" is something a test can check instead of
    something true by construction.

    Rows are top level expense categories; each carries its children. A parent
    with transactions filed on the parent itself gets one extra child row for
    them, or the children would not add up to the parent. Uncategorized is a
    row of its own, since it counts as spend (see _split) and the columns must
    equal the month's spend. Internal categories are excluded, as everywhere."""
    categories = categories or load_categories()
    done = complete_months(conn)
    end = end_month if end_month in done else (done[-1] if done else None)
    months = [m for m in done if m <= end][-ANNUAL_WINDOW_MONTHS:] if end else []

    splits = [_split(_rows_in(conn, m), categories) for m in months]
    per_cat = [s["by_category"] for s in splits]

    def values(ids) -> list[int]:
        return [sum(bc.get(i, 0) for i in ids) for bc in per_cat]

    rows = []
    for top in (c for c in categories.values() if c.parent is None and c.type == "expense"):
        kids = [c for c in categories.values() if c.parent == top.id]
        row = _matrix_row(top.id, top.name, None, values([top.id] + [k.id for k in kids]), months)
        if not row["months_with_spend"]:
            continue
        children = [_matrix_row(k.id, k.name, top.id, values([k.id]), months) for k in kids]
        if kids:
            children.append(_matrix_row(top.id, "Sin subcategoría", top.id,
                                        values([top.id]), months))
            children[-1]["direct"] = True
        row["children"] = sorted((c for c in children if c["months_with_spend"]),
                                 key=lambda c: -c["total_cents"])
        rows.append(row)
    uncategorized = values([None])
    if any(uncategorized):
        rows.append(_matrix_row(UNCATEGORIZED, "Sin categorizar", None, uncategorized, months))
    rows.sort(key=lambda r: -r["total_cents"])

    income_roots = [c.id for c in categories.values() if c.parent is None and c.type == "income"]
    income = _matrix_row(income_roots[0] if len(income_roots) == 1 else None, "Ingresos", None,
                         [s["income_cents"] for s in splits], months)
    totals = _matrix_row(None, "Gasto total", None, [s["spend_cents"] for s in splits], months)
    return {
        "end_month": end,
        "months": months,
        "low_confidence": len(months) < MIN_BASELINE_MONTHS,
        "rows": rows,
        "totals": totals,
        "income": income,
        "grand_total_cents": totals["total_cents"],
    }
