"""The round you run when new exports arrive: import, check, recategorize, and
name what no rule matched.

Both `finanzas update` and the web upload page go through here. The steps are
shared rather than merely similar: two surfaces reporting different things about
the same ledger would be worse than either of them being wrong, because the
CLI's answer is the one that was verified against the real exports and the page
has to be that same answer.

Nothing in this module prints. The command formats the result for a terminal and
the route renders it in a template, which is the only difference between them.
"""

from pathlib import Path

from finanzas import rules as rules_mod
from finanzas.check import internal_transfers, unmasked_digit_runs, walk_chain
from finanzas.ingest import import_file, load_accounts

# D10: a dump folder, not a queue. Nothing is moved, renamed or deleted here, and
# a file whose sha256 is already known is skipped, so exports can stay forever.
INBOX = Path("data/inbox")
SUFFIXES = (".xls", ".xlsx")


def inbox_files() -> list[Path]:
    if not INBOX.exists():
        return []
    return sorted(p for p in INBOX.iterdir() if p.suffix.lower() in SUFFIXES)


def import_files(conn, paths=None) -> list[dict]:
    """Import each file, carrying on past a bad one. A single unreadable export
    must not stop the rest of the inbox: the others are independent, and the
    failure is reported per file rather than as one dead run."""
    accounts = load_accounts()
    results = []
    for path in (paths if paths else inbox_files()):
        try:
            results.append(import_file(conn, Path(path), accounts))
        except Exception as e:
            results.append({"file": Path(path).name, "status": "error", "error": str(e)})
    return results


def ledger(conn) -> list[dict]:
    """Per account: how much is in there, over what range, and where the balance
    chain breaks. walk_chain is run once per account and its result is reused by
    the check below, so the two can never disagree about the same account."""
    out = []
    for (account,) in conn.execute(
            "SELECT DISTINCT account FROM transactions ORDER BY 1"):
        ordered, breaks = walk_chain(conn, account)
        dates = [r["date"] for r in ordered]
        out.append({
            "account": account,
            "n": len(ordered),
            "first": min(dates) if dates else None,
            "last": max(dates) if dates else None,
            "n_imports": conn.execute(
                "SELECT COUNT(*) FROM imports WHERE account=?", (account,)).fetchone()[0],
            "balance_cents": ordered[-1]["balance_cents"] if ordered else 0,
            "breaks": breaks,
        })
    return out


def check(conn, accounts=None) -> dict:
    """Everything that would make a number downstream a lie: a broken balance
    chain (a missing period or a row counted twice, D11), a transfer with one leg
    missing (which invents income and spending at once, D14), and any long digit
    run that reached the database (C6)."""
    accounts = ledger(conn) if accounts is None else accounts
    transfers = internal_transfers(conn)
    leaks = unmasked_digit_runs(conn)
    problems = (sum(len(a["breaks"]) for a in accounts)
                + len(transfers["unpaired_out"]) + len(transfers["unpaired_in"]))
    return {"accounts": accounts, "transfers": transfers, "leaks": leaks,
            "problems": problems, "ok": not problems and not leaks}


def run(conn, paths=None) -> dict:
    """Import, look at the ledger, recategorize, and state what is left over.

    Categorization runs even when the check found something, which is deliberate:
    the categories are derived from the files and recomputing them changes
    nothing about the problem. What must not happen is the problem going
    unreported, so `ok` travels with the result and both surfaces lead with it."""
    imports = import_files(conn, paths)
    accounts = ledger(conn)
    checked = check(conn, accounts)
    categorized = rules_mod.apply_rules(conn, rules_mod.load())
    pending = conn.execute(
        "SELECT COUNT(*) AS n, COUNT(DISTINCT description_norm) AS groups "
        "FROM transactions WHERE category IS NULL").fetchone()
    return {
        "imports": imports,
        "failed": sum(r["status"] == "error" for r in imports),
        "check": checked,
        "categorize": categorized,
        # The handoff point: the app names the gap and stops. Extending
        # rules.yaml happens in Claude Code, never in the app (C2).
        "pending": {"n": pending["n"], "groups": pending["groups"]},
    }
