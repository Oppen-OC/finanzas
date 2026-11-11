"""Command line entry point: uv run python -m finanzas <command>."""

import argparse
import contextlib
import csv
import json
import sys
from datetime import date, datetime
from pathlib import Path

from finanzas import db
from finanzas import (goals as goals_mod, manual as manual_mod, metrics as metrics_mod,
                      projection as projection_mod, review as review_mod, rules as rules_mod,
                      scenarios as scenarios_mod, update as update_mod)
# Shared with the web UI: two euro formatters drift, and a dashboard that rounds
# differently from the terminal reads as a bug in the ledger. `cents` is the same
# parser rules.yaml uses, so a limit and a rule read an amount identically.
from finanzas.fmt import cents as parse_cents, eur, eur_whole


# The print_* helpers below are the only place each of these results is put into
# words. `import`, `stats`, `check` and `update` all render through them, so the
# monthly command cannot describe the ledger differently from the command that
# looks at one part of it.
def print_imports(results: list[dict]) -> int:
    for res in results:
        if res["status"] == "error":
            print(f"ERROR {res['file']}: {res['error']}")
        elif res["status"] == "imported":
            print(f"{res['file']} -> {res['account']}: {res['rows']} rows, "
                  f"{res.get('inserted', 0)} new, {res.get('duplicate', 0)} duplicate")
        else:
            print(f"{res['file']}: {res['status']}")
    return 1 if any(r["status"] == "error" for r in results) else 0


def print_ledger(accounts: list[dict]) -> None:
    for a in accounts:
        print(f"{a['account']}: {a['n']} transactions, {a['first']} .. {a['last']}, "
              f"{a['n_imports']} import(s), latest balance {eur(a['balance_cents'])} EUR"
              + (f", {len(a['breaks'])} chain break(s)" if a["breaks"] else ""))


def print_check(checked: dict) -> int:
    for a in checked["accounts"]:
        print(f"{a['account']}: {a['n']} transactions, {len(a['breaks'])} chain break(s)")
        for before, after, diff in a["breaks"]:
            print(f"  break between {before['date_booked']} {before['description'][:40]!r} "
                  f"(balance {eur(before['balance_cents'])}) and {after['date_booked']} "
                  f"{after['description'][:40]!r}: {eur(diff)} EUR unaccounted "
                  "(rows missing between these two, or a row counted twice)")
    t = checked["transfers"]
    print(f"internal transfers: {len(t['paired'])} paired, {len(t['unpaired_out'])} outgoing "
          f"and {len(t['unpaired_in'])} incoming leg(s) alone, "
          f"{len(t['suspects'])} mirrored pair(s) not marked internal")
    for r in t["unpaired_out"] + t["unpaired_in"]:
        print(f"  lone leg {r['date']} {r['account']:<10} {eur(r['amount_cents']):>10}  "
              f"{r['description'][:60]}")
    for a, b in t["suspects"]:
        print(f"  not marked internal? {a['date']} {a['account']} {eur(a['amount_cents'])} "
              f"{a['description'][:32]!r} <-> {b['date']} {b['account']} {b['description'][:32]!r}")
    leaks = checked["leaks"]
    print(f"unmasked long digit runs: {len(leaks)}"
          + (f" in {sorted(set(leaks))}" if leaks else ""))
    return 0 if checked["ok"] else 1


def print_categorize(res: dict) -> None:
    share = f" ({100 * res['uncategorized'] / res['total']:.1f}%)" if res["total"] else ""
    print(f"assigned {res['assigned']} by rule, {res['manual']} manual, "
          f"{res['uncategorized']} uncategorized of {res['total']}{share}")
    for rule_id, n in res["by_rule"].items():
        print(f"  {n:>4}  {rule_id}")
    for key in res["stale_manual"]:
        print(f"  WARNING manual.yaml entry {key} matches no transaction")


def cmd_import(conn, args) -> int:
    return print_imports(update_mod.import_files(conn, args.paths))


def cmd_stats(conn, args) -> int:
    print_ledger(update_mod.ledger(conn))
    return 0


def cmd_check(conn, args) -> int:
    return print_check(update_mod.check(conn))


def cmd_list(conn, args) -> int:
    # The filter logic lives in metrics.find_transactions, shared with the web
    # page: two copies of "month plus category plus search" drift apart.
    rows = metrics_mod.find_transactions(
        conn, account=args.account, month=args.month, search=args.search,
        category=args.category, limit=args.limit)
    for r in rows:
        # The id is here so it can be pasted straight into `categorize --set`.
        print(f"{r['id']:>5}  {r['date']}  {r['account']:<10} {eur(r['amount_cents']):>10}  "
              f"{(r['category'] or '-'):<22} {r['description'][:64]}")
    print(f"{len(rows)} transaction(s), net {eur(sum(r['amount_cents'] for r in rows))} EUR")
    return 0


def cmd_categorize(conn, args) -> int:
    """Reapply rules.yaml from scratch (idempotent by construction: rule-assigned
    categories are cleared first, manual ones are never touched)."""
    if args.set:
        txn_id, category = args.set
        res = manual_mod.set_category(conn, int(txn_id), category, note=args.note)
        print(f"manual.yaml: {res['key']} -> {res['category']}")
    res = rules_mod.apply_rules(conn, rules_mod.load(), dry_run=args.dry_run)
    if args.dry_run:
        share = f" ({100 * res['uncategorized'] / res['total']:.1f}%)" if res["total"] else ""
        print(f"would assign {res['assigned']} by rule, {res['manual']} manual, "
              f"{res['uncategorized']} uncategorized of {res['total']}{share}")
        for rule_id, n in res["by_rule"].items():
            print(f"  {n:>4}  {rule_id}")
        for key in res["stale_manual"]:
            print(f"  WARNING manual.yaml entry {key} matches no transaction")
    else:
        print_categorize(res)
    return 0


def cmd_update(conn, args) -> int:
    """The monthly command: import whatever is new, check the ledger, recategorize.

    It ends by naming what the rules did not recognize, because that is the only
    point where a human (or Claude, in Claude Code) has to do anything. The app
    itself never calls a model: it states the gap and stops.

    The work is in update.run, which the web upload page calls too. This function
    is the terminal's view of that one result."""
    res = update_mod.run(conn, args.paths)
    print("== import ==")
    print_imports(res["imports"])
    print("\n== ledger ==")
    print_ledger(res["check"]["accounts"])
    print("\n== check ==")
    print_check(res["check"])
    print("\n== categorize ==")
    print_categorize(res["categorize"])

    print()
    if res["pending"]["n"]:
        print(f"{res['pending']['n']} transaction(s) in {res['pending']['groups']} new "
              "description(s) match no rule.\n"
              "  Run /finanzas in Claude Code to extend rules.yaml, or "
              "`uncategorized` to look first.")
    else:
        print("every transaction is categorized.")
    return 1 if (res["failed"] or not res["check"]["ok"]) else 0



def pace_cell(projected: bool, amount_cents: int) -> str:
    """The projection column, in both the per-category rows and the total, so the
    two line up and the total cannot claim a projection the rows did not make."""
    return (f"projected {eur(amount_cents):>10}" if projected
            else f"{'landed':>10} {eur(amount_cents):>9}")


def pct(value, width: int = 7) -> str:
    """None means the metric could not be computed, which is a real answer and
    must not be printed as 0%."""
    return f"{'n/a' if value is None else f'{value:.1%}':>{width}}"


def cmd_report(conn, args) -> int:
    """Every milestone 3 metric for one month. All the arithmetic lives in
    metrics.py; this function only lays it out, so the web UI can render the
    same numbers without reimplementing any of them."""
    cats = metrics_mod.load_categories()
    month = args.month or conn.execute(
        "SELECT MAX(substr(date, 1, 7)) FROM transactions").fetchone()[0]
    if month is None:
        print("empty ledger")
        return 1
    done = metrics_mod.complete_months(conn)
    window = metrics_mod.coverage(conn)

    summary = metrics_mod.monthly_summary(conn, month, cats)
    print(f"== {month} ==   ledger covers {window[0]} .. {window[1]}, "
          f"{len(done)} complete month(s)")
    if not summary["complete"]:
        print(f"   {month} is PARTIAL: charges dated later in the month are not in "
              "yet, so every figure below is an incomplete month, not a low one.")
    if summary["low_confidence"]:
        print(f"   LOW CONFIDENCE: baselines rest on {len(summary['baseline_months'])} "
              f"complete month(s) {summary['baseline_months']}, fewer than "
              f"{metrics_mod.MIN_BASELINE_MONTHS}. Medians and deviations are "
              "directional at best.")

    print(f"\n  income {eur(summary['income_cents']):>12}    "
          f"spend {eur(summary['spend_cents']):>12}    "
          f"net {eur(summary['net_cents']):>12}    ({summary['n_transactions']} txn)")

    print(f"\n-- by category --   (median over {summary['baseline_months'] or 'no baseline'})")
    for line in summary["by_category"]:
        median = eur(line["median_cents"]) if line["median_cents"] is not None else "-"
        dev = f"{line['deviation_pct']:+.0f}%" if line["deviation_pct"] is not None else "-"
        print(f"  {line['category'] or '(sin categorizar)':<26} {eur(line['amount_cents']):>11}"
              f"   median {median:>10}  {dev:>8}")

    pace = metrics_mod.month_to_date(conn, month, None, cats)
    if pace["lines"]:
        print(f"\n-- pace --   {pace['days_elapsed']} of {pace['days_in_month']} day(s) "
              f"as of {pace['as_of']} (the ledger's end, not today)")
        for line in pace["lines"]:
            mark = "OVER" if line["over"] else ("over projected" if line["over_projected"] else "")
            print(f"  {line['category']:<26} {eur(line['spent_cents']):>11} of "
                  f"{eur(line['limit_cents']):>9}   "
                  + pace_cell(line["projected"], line["projected_cents"])
                  + f"  {line['projected_share']:>6.0%}  {mark}".rstrip())
        word = "projected" if any(line["projected"] for line in pace["lines"]) else "landed"
        print(f"  {'total':<26} {eur(pace['spent_cents']):>11} of {eur(pace['limit_cents']):>9}"
              "   " + pace_cell(word == "projected", pace["projected_cents"])
              + f"  {pace['n_over']} over")

    split = metrics_mod.fixed_vs_variable(conn, month, cats)
    total = split["total_cents"]
    print(f"\n-- fixed vs variable --")
    print(f"  fixed      {eur(split['fixed_cents']):>11}  "
          f"{split['fixed_cents'] / total if total else 0:>6.1%}")
    print(f"  variable   {eur(split['variable_cents']):>11}  "
          f"{split['variable_cents'] / total if total else 0:>6.1%}")
    for desc, amount in split["fixed_items"]:
        print(f"      {eur(amount):>9}  {desc[:56]}")

    recurring = metrics_mod.find_recurring(conn, cats)
    live = [r for r in recurring if r["active"]]
    print(f"\n-- recurring --   {len(live)} live, "
          f"{eur(sum(r['annual_cents'] for r in live))} EUR/year")
    for r in recurring:
        print(f"  {eur(r['amount_cents']):>9} {r['cadence']:<8} "
              f"{eur(r['annual_cents']):>9}/yr  {r['occurrences']}x every "
              f"{r['median_gap_days']:.0f}d, last {r['last_seen']}"
              f"{'' if r['active'] else '  STALE, probably cancelled'}\n"
              f"            {r['category']:<24} {r['description_norm'][:44]}")
    if not recurring:
        print(f"  none: a series needs {metrics_mod.MIN_OCCURRENCES} charges on a "
              "steady cadence, which this much history cannot yet show.")

    saving = metrics_mod.savings_rate(conn, month, cats)
    disc = metrics_mod.discretionary_share(conn, month, cats)
    print(f"\n-- rates --")
    print(f"  savings rate       {pct(saving['rate'])}   "
          f"rolling {len(saving['rolling_months'])}m {pct(saving['rolling_rate'])}"
          + ("   (no income this month)" if saving["rate"] is None else ""))
    print(f"  discretionary      {pct(disc['share'])}   "
          f"{eur(disc['discretionary_cents'])} of {eur(disc['spend_cents'])}")

    run = metrics_mod.runway(conn, args.liquid_cents, cats)
    burn = metrics_mod.burn_rate(conn, cats)
    print(f"\n-- burn and runway --")
    # One line per sample stopped being readable at a 12 month window; the
    # spread is what the reader needs, and the months go underneath in rows.
    amounts = sorted(v for _, v in burn["samples"])
    print(f"  burn rate (median) {eur(burn['median_cents']):>11} /month  over "
          f"{len(burn['samples'])} month(s), low {eur(amounts[0])}, high {eur(amounts[-1])}")
    for i in range(0, len(burn["samples"]), 4):
        print("      " + "   ".join(f"{m} {eur(v):>9}" for m, v in burn["samples"][i:i + 4]))
    print(f"  liquid             {eur(run['liquid_cents']):>11}  ({run['liquid_source']}: "
          + ", ".join(f"{a} {eur(b)}" for a, b in run["per_account"].items()) + ")")
    print(f"  runway             "
          + (f"{run['months']:>11.1f}  months" if run["months"] is not None
             else "        n/a  (no positive burn rate to divide by)"))
    if run["low_confidence"]:
        print("  Treat the runway as an order of magnitude: the median is taken over "
              f"{len(burn['months'])} month(s), so a single unusual purchase still "
              "moves it.")
    return 0


def cmd_year(conn, args) -> int:
    """The annual matrix in the terminal: top level rows only, whole euros. The
    page expands the children; eighty-odd columns of terminal do not."""
    m = metrics_mod.annual_matrix(conn, args.end)
    if not m["months"]:
        print("no complete month yet: the matrix needs at least one month the ledger "
              "covers end to end")
        return 1
    if args.end and args.end != m["end_month"]:
        print(f"{args.end} is not a complete month; showing the window ending {m['end_month']}")
    print(f"== {m['months'][0]} .. {m['months'][-1]} ==   {len(m['months'])} complete month(s), "
          "whole euros")
    if m["low_confidence"]:
        print(f"   LOW CONFIDENCE: fewer than {metrics_mod.MIN_BASELINE_MONTHS} complete months, "
              "so the row medians are not worth reading.")
    w = 7
    head = f"{'':<22}" + "".join(f"{mo[2:]:>{w}}" for mo in m["months"])
    print("\n" + head + f"{'total':>9}{'median':>8}{'n':>4}")

    def line(row, label):
        return (f"{label[:21]:<22}" + "".join(f"{eur_whole(c['cents']):>{w}}" for c in row["cells"])
                + f"{eur_whole(row['total_cents']):>9}{eur_whole(row['median_cents']):>8}"
                + f"{row['months_with_spend']:>4}")

    for row in m["rows"]:
        print(line(row, row["id"] if row["id"] != metrics_mod.UNCATEGORIZED else "(sin categorizar)"))
    print("-" * len(head) + "-" * 21)
    print(line(m["totals"], "spend"))
    print(line(m["income"], "income"))
    print("\n  n = months with a nonzero amount: 12 of 12 is a habit, 1 is a one-off.")
    return 0


def cmd_project(conn, args) -> int:
    """The scenarios in scenarios.yaml, month by month (D31). All the arithmetic
    is projection.py's; this lays it out, and the Escenarios page renders the
    same dicts."""
    try:
        config = scenarios_mod.load()
    except ValueError as e:
        print(f"scenarios.yaml is not valid: {e}")
        return 1
    if config is None:
        print(f"no {scenarios_mod.SCENARIOS_PATH} yet. Create it at the repo root; the format "
              "is documented in docs/DESIGN.md (D31) and at the top of the starter file.")
        return 1
    ids = [s.id for s in config.scenarios]
    if args.scenario and args.scenario not in ids:
        print(f"unknown scenario {args.scenario!r}; scenarios.yaml has {', '.join(ids)}")
        return 1
    liquid = parse_cents(args.liquid, "--liquid") if args.liquid is not None else None
    res = projection_mod.project_all(conn, config, liquid_cents=liquid)
    st = res["start"]
    if st is None:
        print("nothing to project from: the ledger needs at least one complete month for a "
              "burn rate")
        return 1

    print(f"== projection from {st['as_of']} (the ledger's end, not today) ==")
    print(f"   liquid {eur(st['liquid_cents'])} ({st['liquid_source']}) · burn "
          f"{eur(st['burn_cents'])}/month over {len(st['burn_months'])} complete month(s) · "
          f"floor {eur(res['floor_cents'])} · {res['horizon_months']} month(s)")
    if st["stub_month"]:
        print(f"   {st['stub_month']}* is a stub: the {st['stub_days_left']} of "
              f"{st['stub_days_in_month']} day(s) the ledger has not reached, at the base "
              "rate, with no events.")
    if st["low_confidence"]:
        print(f"   LOW CONFIDENCE: the burn rests on {len(st['burn_months'])} complete "
              f"month(s), fewer than {metrics_mod.MIN_BASELINE_MONTHS}.")

    print(f"\n  {'scenario':<20}{'< floor':>9}{'< 0':>9}{'needs/mo':>12}{'gap/mo':>12}"
          f"{'final':>13}")
    for s in res["scenarios"]:
        print(f"  {s['id'][:19]:<20}{s['floor_month'] or '-':>9}{s['zero_month'] or '-':>9}"
              f"{eur(s['required_income_cents']):>12}{eur(s['gap_cents']):>12}"
              f"{eur(s['final_cents']):>13}")
    print("\n  needs/mo: net income that stops the decline once only open-ended events are left."
          "\n  gap/mo:   that, minus the open-ended income the scenario already has "
          "(negative is a surplus).")

    for s in res["scenarios"]:
        if args.scenario and s["id"] != args.scenario:
            continue
        print(f"\n-- {s['id']} · {s['name']} --")
        print(f"  {'month':<9}{'opening':>12}{'income':>11}{'outflow':>11}{'closing':>12}   events")
        for r in s["rows"]:
            mark = ("  < 0" if r["closing_cents"] < 0 else
                    "  < floor" if r["closing_cents"] < res["floor_cents"] else "")
            notes = ", ".join(f"{e['label']} {'+' if e['cents'] > 0 else ''}{eur(e['cents'])}"
                              for e in r["events"])
            if r["replaced"]:
                notes += f" (base without {', '.join(r['replaced'])} {eur(r['base_cents'])})"
            print(f"  {r['month'] + ('*' if r['partial'] else ''):<9}"
                  f"{eur(r['opening_cents']):>12}{eur(r['income_cents']):>11}"
                  f"{eur(r['outflow_cents']):>11}{eur(r['closing_cents']):>12}{mark:<9}"
                  f" {notes}".rstrip())
        for k in s["skipped"]:
            why = ("ended before the first projected month, so it is in the ledger or it "
                   "never happened" if k["why"] == "past" else "starts after the horizon")
            print(f"  skipped: {k['label']} ({k['month']}): {why}")
    return 0


def cmd_review(conn, args) -> int:
    """The monthly review's numbers: the score and what is worth talking about. All
    of it is computed in review.py; `/asesor` reads the --json form and writes the
    commentary, and never recomputes a figure."""
    month = args.month or review_mod.latest_complete(conn)
    if month is None:
        print("no complete month in the ledger yet")
        return 1
    months = metrics_mod.previous_months(month, args.months - 1) + [month] \
        if args.months > 1 else [month]
    cats = metrics_mod.load_categories()
    results = [review_mod.month_review(conn, m, cats) for m in months]
    if args.json:
        print(json.dumps(results if args.months > 1 else results[0], indent=2, ensure_ascii=False))
        return 0
    for r in results:
        head = f"{r['score']:.1f}/10" if r["score"] is not None else "no score"
        print(f"== {r['month']} ==  {head}"
              + ("  LOW CONFIDENCE" if r["low_confidence"] else ""))
        for reason in r["blocked"]:
            print(f"   not scored: {reason}")
        for name, c in r["components"].items():
            print(f"   {name:<14} {c['points']:4.1f}  x {c['weight']:.0%}   value {c['value']:.2f}")
        print(f"   spend {eur(r['spend_cents'])}   income {eur(r['income_cents'])}   "
              f"opaque {r['opaque_share']:.0%} of spend")
        for b in r["big_charges"]:
            print(f"   big    {eur(b['amount_cents']):>10}  {b['date']}  {b['category'] or '-'}"
                  f"  {b['description'][:40]}")
        for d in r["deviations"]:
            print(f"   over   {d['category'] or '(sin categorizar)':<24} {eur(d['amount_cents']):>10}"
                  f"  median {eur(d['median_cents'])}")
        for p in r["price_changes"]:
            print(f"   price  {p['description'][:40]}  {eur(p['amount_cents'])} "
                  f"(usually {eur(p['usual_cents'])})")
    return 0


def cmd_goals(conn, args) -> int:
    """Read and write goals.yaml: the limits you set yourself, and the liquid
    balance override the runway uses.

    `--since` defaults to the current calendar month, and this is the one place
    in the project where the wall clock is the right clock: setting a limit is a
    decision you are making today, while every *measurement* is pinned to the
    end of the ledger instead, because a measurement must not change with the
    day it is run."""
    cats = metrics_mod.load_categories()
    since = args.since or date.today().isoformat()[:7]

    if args.liquid is not None:
        value = (None if args.liquid.strip().lower() in ("none", "auto", "cuentas")
                 else parse_cents(args.liquid, "--liquid"))
        goals_mod.set_liquid(value, categories=cats)
        print("liquid: " + ("the sum of account balances" if value is None
                            else f"{eur(value)} EUR (override)"))
    if args.set:
        category, amount = args.set
        goals_mod.set_limit(category, parse_cents(amount, "--set"), since,
                            note=args.note, categories=cats)
        print(f"limit: {category} {eur(parse_cents(amount, '--set'))} EUR/month from {since}")
    if args.unset:
        goals_mod.unset_limit(args.unset, since, categories=cats)
        print(f"limit: {args.unset} lifted from {since}")

    goals = goals_mod.load(categories=cats)
    month = args.month or date.today().isoformat()[:7]
    in_force = goals.at(month)
    print(f"\n{len(in_force)} limit(s) in force in {month}, {goals_mod.GOALS_PATH}")
    for cid, limit in sorted(in_force.items(), key=lambda kv: -kv[1].monthly_cents):
        history = goals.history(cid)
        older = f"   ({len(history) - 1} earlier)" if len(history) > 1 else ""
        print(f"  {eur(limit.monthly_cents):>10} /month  {cid:<26} since {limit.since}{older}"
              + (f"   {limit.note}" if limit.note else ""))
    print(f"  liquid override: "
          + (f"{eur(goals.liquid_cents)} EUR" if goals.liquid_cents is not None
             else "none, the runway uses the sum of account balances"))
    return 0


def cmd_backup(conn, args) -> int:
    """Timestamped copy of the ledger.

    Through sqlite3's own backup API rather than a file copy: a plain copy of a
    database with a live connection can be torn halfway through a page, and the
    copy that fails is the one you only ever read back in an emergency.

    Worth being honest about what this is for. The database is derived: it can
    be rebuilt from data/inbox plus rules.yaml, manual.yaml and categories.yaml,
    which is what actually keeps the history safe, and those live in git. A
    backup buys a fast restore and a frozen snapshot of the state that produced
    a given report. It is not the durability story."""
    import sqlite3

    # sqlite3's backup retries a busy source forever and takes no timeout, and an
    # uncommitted write on this very connection is a busy source: not a slow
    # backup, a command that never returns and never says why. Nothing in the CLI
    # leaves a transaction open, but a caller that did would hang here.
    conn.commit()
    out = Path(args.dir or "data/backups")
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = out / f"finanzas-{stamp}.db"
    # closing(), not the connection's own `with`: that one commits and leaves the
    # file open, which on Windows keeps the copy locked until garbage collection.
    with contextlib.closing(sqlite3.connect(target)) as dest:
        conn.backup(dest)
    n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    print(f"{target}  {target.stat().st_size / 1024:.0f} KB, {n} transactions")
    print("Restore by copying it over data/finanzas.db, or rebuild from the inbox: "
          "the files, not this copy, are what the ledger is reproducible from.")
    return 0


def cmd_rules(conn, args) -> int:
    rules = rules_mod.load()
    stats = rules_mod.diagnose(conn, rules)
    dead = [s for s in stats if s["would_match"] == 0]
    shadowed = [s for s in stats if s["would_match"] and not s["assigned"]]
    for s in stats:
        mark = "DEAD    " if s in dead else ("SHADOWED" if s in shadowed else "        ")
        print(f"{s['assigned']:>5}{s['would_match']:>7}  {mark}  {s['id']:<28} -> {s['category']}")
    print(f"\n{len(rules)} rules, {len(dead)} dead (match nothing), "
          f"{len(shadowed)} shadowed (everything they match is taken by an earlier rule)")
    for s in dead + shadowed:
        print(f"  {'dead' if s in dead else 'shadowed'}: {s['id']}")
    return 1 if dead or shadowed else 0


def cmd_uncategorized(conn, args) -> int:
    """Input for the categorization loop (D7): what the rules miss, one line per
    distinct description_norm, biggest money first so the rules that matter most get
    written first. Printed as description_norm because that is what rules match on."""
    total, missing = conn.execute(
        "SELECT COUNT(*), SUM(category IS NULL) FROM transactions").fetchone()
    # --include-misc brings back what a rule deliberately parked in misc, for the
    # periodic pass over movements that were unreadable at the time.
    pending = "category IS NULL" + (" OR category LIKE 'misc%'" if args.include_misc else "")
    groups = conn.execute(
        "SELECT description_norm, COUNT(*) AS n, SUM(amount_cents) AS total, "
        "MIN(date) AS first, MAX(date) AS last, GROUP_CONCAT(DISTINCT account) AS accounts "
        f"FROM transactions WHERE {pending} GROUP BY description_norm "
        "ORDER BY ABS(SUM(amount_cents)) DESC, n DESC, description_norm").fetchall()
    print(f"uncategorized: {missing or 0} of {total} transactions "
          f"({100 * (missing or 0) / total if total else 0:.1f}%), {len(groups)} distinct descriptions")
    for g in groups[:args.limit] if args.limit else groups:
        span = g["first"] if g["first"] == g["last"] else f"{g['first']}..{g['last']}"
        print(f"{g['n']:>4}x {eur(g['total']):>11}  {span:<22} {g['accounts']:<10} {g['description_norm']}")
    return 0


def cmd_serve(conn, args) -> int:
    """Run the local web UI (milestone 4).

    A command rather than a documented `uvicorn ...` line because every path in
    the app is relative to the repo root: categories.yaml, rules.yaml,
    manual.yaml and data/finanzas.db. A server started from anywhere else finds
    none of them and fails in a way that looks like a code bug.

    Bound to the loopback interface on purpose. There is no auth (C3), so the
    only thing keeping the ledger private is that nothing off this machine can
    reach the port."""
    import uvicorn

    print(f"finanzas on http://{args.host}:{args.port}  (ctrl-c to stop)")
    uvicorn.run("finanzas.web.app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_export(conn, args) -> int:
    cols = ["account", "date", "date_booked", "amount_cents", "balance_cents", "description"]
    out = open(args.out, "w", newline="", encoding="utf-8") if args.out else sys.stdout
    w = csv.writer(out)
    w.writerow(cols)
    for r in conn.execute(f"SELECT {', '.join(cols)} FROM transactions ORDER BY account, date, id"):
        w.writerow(list(r))
    if args.out:
        out.close()
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog="finanzas")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("import", help="import exports (default: everything in data/inbox)")
    s.add_argument("paths", nargs="*")
    sub.add_parser("stats", help="per-account summary")
    sub.add_parser("check", help="balance chain and masking checks")
    sub.add_parser("rules", help="per-rule match counts, dead and shadowed rules")
    s = sub.add_parser("list", help="recent transactions")
    s.add_argument("--account")
    s.add_argument("--month", help="YYYY-MM")
    s.add_argument("--search", help="substring of the description, e.g. bizum")
    s.add_argument("--category", help="category id; a parent id includes its children")
    s.add_argument("--limit", type=int, default=50)
    s = sub.add_parser("update", help="import + check + categorize, the monthly command")
    s.add_argument("paths", nargs="*")
    s = sub.add_parser("categorize", help="apply rules.yaml to the ledger")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--set", nargs=2, metavar=("TXN_ID", "CATEGORY"),
                   help="pin one transaction to a category in manual.yaml")
    s.add_argument("--note", help="what --set should write in the entry's note")
    s = sub.add_parser("uncategorized", help="uncategorized transactions grouped by description")
    s.add_argument("--limit", type=int, default=0, help="show only the N largest groups")
    s.add_argument("--include-misc", action="store_true",
                   help="also list what rules parked in misc")
    s = sub.add_parser("report", help="every metric for one month")
    s.add_argument("--month", help="YYYY-MM, default the latest month with data")
    s.add_argument("--liquid", dest="liquid_cents", type=lambda v: round(float(v) * 100),
                   help="liquid balance in euros, overriding the sum of account balances "
                        "when part of it is not really available cash (O2)")
    s = sub.add_parser("year", help="category x month spend over the last 12 complete months")
    s.add_argument("--end", metavar="YYYY-MM", help="last month of the window, default the "
                   "latest complete month")
    s = sub.add_parser("project", help="balance month by month under scenarios.yaml")
    s.add_argument("--scenario", metavar="ID", help="print the monthly table for this one only")
    s.add_argument("--liquid", metavar="EUR",
                   help="starting balance, overriding goals.yaml and the account sum")
    s = sub.add_parser("review", help="score and talking points for a month (/asesor)")
    s.add_argument("--month", help="YYYY-MM, default the last complete month")
    s.add_argument("--months", type=int, default=1,
                   help="review this many months ending at --month")
    s.add_argument("--json", action="store_true", help="machine-readable, for /asesor")
    s = sub.add_parser("serve", help="run the local web UI")
    s.add_argument("--host", default="127.0.0.1", help="loopback only unless you mean it")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--reload", action="store_true", help="restart on code changes")
    s = sub.add_parser("goals", help="limits and settings, stored in goals.yaml")
    s.add_argument("--set", nargs=2, metavar=("CATEGORY", "EUR"),
                   help="monthly limit for a category, in euros")
    s.add_argument("--unset", metavar="CATEGORY", help="lift a category's limit")
    s.add_argument("--since", metavar="YYYY-MM",
                   help="month the change starts applying, default this month")
    s.add_argument("--note", help="what --set writes in the entry's note")
    s.add_argument("--liquid", metavar="EUR",
                   help="liquid balance the runway uses, or 'none' for the account sum")
    s.add_argument("--month", metavar="YYYY-MM", help="which month's limits to print")
    s = sub.add_parser("backup", help="timestamped copy of the ledger")
    s.add_argument("dir", nargs="?", help="destination folder, default data/backups")
    s = sub.add_parser("export", help="transactions as CSV")
    s.add_argument("out", nargs="?")
    args = p.parse_args()

    handlers = {"import": cmd_import, "update": cmd_update, "stats": cmd_stats, "check": cmd_check,
                "list": cmd_list, "categorize": cmd_categorize, "rules": cmd_rules, "report": cmd_report, "year": cmd_year, "project": cmd_project, "review": cmd_review, "goals": cmd_goals, "backup": cmd_backup, "serve": cmd_serve, "uncategorized": cmd_uncategorized, "export": cmd_export}
    sys.exit(handlers[args.cmd](db.connect(), args))
