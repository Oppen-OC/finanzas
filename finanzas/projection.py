"""Month by month balance under the scenarios in scenarios.yaml (D31).

Arithmetic over stated events, not a forecast. The only number taken from the
ledger is the burn rate the app already computes (D19); income enters only
through events, because personal income is often an event and not a baseline
(the argument D29 made for the score).

Pure functions returning plain dicts, like metrics.py: the CLI and the page
render the same result and compute nothing of their own (D22).

Where the projection starts. "Now" is the end of the ledger, coverage()[1], not
the wall clock (D27), and the starting balance is the liquid figure runway uses,
measured at that same point. That balance has already paid for the part of the
month the ledger covers, so the first row is a stub for the rest of that month:
the base outflow prorated by the days left, and no events, since the ledger may
already hold that month's income or rent and there is no way to tell. Full months
follow, `horizon_months` of them. An event that ends before the first full month
is reported as skipped rather than applied or refused: it is in the ledger now,
or it never happened, and either way the next import should not break the page.
"""

from datetime import date

from finanzas import goals as goals_mod, metrics
from finanzas.categories import Category, load as load_categories
from finanzas.scenarios import Config, Scenario


def next_month(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    return f"{y + (m == 12):04d}-{1 if m == 12 else m + 1:02d}"


def _prorate(cents: int, days: int, of: int) -> int:
    # Integer half-up, so the stub is cents like everything else (C8).
    return (2 * cents * days + of) // (2 * of)


def starting_point(conn, categories: dict[str, Category] | None = None,
                   goals: goals_mod.Goals | None = None,
                   liquid_cents: int | None = None) -> dict | None:
    """The balance, the burn and the calendar every scenario starts from. None
    when there is nothing to start from: an empty ledger, or no complete month
    yet and therefore no burn rate."""
    categories = categories or load_categories()
    window = metrics.coverage(conn)
    if window is None:
        return None
    run = metrics.runway(conn, liquid_cents, categories, goals)
    if run["burn_cents"] is None:
        return None
    as_of = date.fromisoformat(window[1])
    month = window[1][:7]
    days_in_month = metrics.month_end(month).day
    days_left = days_in_month - as_of.day
    return {
        "as_of": window[1],
        "liquid_cents": run["liquid_cents"],
        "liquid_source": run["liquid_source"],
        "burn_cents": run["burn_cents"],
        "burn_months": metrics.burn_rate(conn, categories)["months"],
        "low_confidence": run["low_confidence"],
        "runway_months": run["months"],
        # No stub when the ledger ends on the last day of a month.
        "stub_month": month if days_left else None,
        "stub_days_left": days_left,
        "stub_days_in_month": days_in_month,
        "first_month": next_month(month),
    }


def project(conn, scenario: Scenario, config: Config, start: dict,
            categories: dict[str, Category] | None = None,
            _bases: dict | None = None) -> dict:
    categories = categories or load_categories()
    bases = _bases if _bases is not None else {}

    def base(replaced: frozenset[str]) -> int:
        # burn_rate with the replaced categories taken out of every month before
        # the median, not burn minus the category's median: medians do not
        # subtract, and only the first is the burn of a life without that cost.
        if replaced not in bases:
            bases[replaced] = (start["burn_cents"] if not replaced else
                               metrics.burn_rate(conn, categories, replaced)["median_cents"])
        return bases[replaced]

    rows, balance = [], start["liquid_cents"]
    if start["stub_month"]:
        outflow = _prorate(base(frozenset()), start["stub_days_left"], start["stub_days_in_month"])
        rows.append({"month": start["stub_month"], "partial": True,
                     "days": start["stub_days_left"], "opening_cents": balance,
                     "income_cents": 0, "base_cents": outflow, "outflow_cents": outflow,
                     "closing_cents": balance - outflow, "events": [], "replaced": []})
        balance -= outflow

    months, m = [], start["first_month"]
    for _ in range(config.horizon_months):
        months.append(m)
        m = next_month(m)

    for month in months:
        active = [e for e in scenario.events if e.active(month)]
        replaced = frozenset(e.replaces for e in active if e.replaces)
        b = base(replaced)
        income = sum(e.cents for e in active if e.cents > 0)
        outflow = b - sum(e.cents for e in active if e.cents < 0)
        rows.append({"month": month, "partial": False, "days": None,
                     "opening_cents": balance, "income_cents": income, "base_cents": b,
                     "outflow_cents": outflow, "closing_cents": balance + income - outflow,
                     "events": [{"label": e.label, "cents": e.cents} for e in active],
                     "replaced": sorted(replaced)})
        balance += income - outflow

    skipped = []
    for e in scenario.events:
        if e.end is not None and e.end < start["first_month"]:
            skipped.append({"label": e.label, "why": "past",
                            "month": e.end})
        elif e.start is not None and e.start > months[-1]:
            skipped.append({"label": e.label, "why": "beyond_horizon", "month": e.start})

    steady = [e for e in scenario.events if e.steady]
    steady_out = (base(frozenset(e.replaces for e in steady if e.replaces))
                  - sum(e.cents for e in steady if e.cents < 0))
    steady_in = sum(e.cents for e in steady if e.cents > 0)

    def first_below(threshold: int) -> str | None:
        return next((r["month"] for r in rows if r["closing_cents"] < threshold), None)

    return {
        "id": scenario.id,
        "name": scenario.name,
        "rows": rows,
        "floor_month": first_below(config.floor_cents),
        "zero_month": first_below(0),
        "final_cents": balance,
        "min_cents": min(r["closing_cents"] for r in rows),
        # What the balance needs every month, from any source, to stop falling
        # once only the open-ended events are left. With no salary in the
        # scenario, this is the net salary that breaks even.
        "required_income_cents": steady_out,
        "steady_income_cents": steady_in,
        # steady outflow minus steady recurring income: positive is how much a
        # month still falls short, negative is a monthly surplus.
        "gap_cents": steady_out - steady_in,
        "skipped": skipped,
        "low_confidence": start["low_confidence"],
    }


def project_all(conn, config: Config, categories: dict[str, Category] | None = None,
                goals: goals_mod.Goals | None = None,
                liquid_cents: int | None = None) -> dict:
    categories = categories or load_categories()
    start = starting_point(conn, categories, goals, liquid_cents)
    out = {"start": start, "floor_cents": config.floor_cents,
           "horizon_months": config.horizon_months, "scenarios": []}
    if start is None:
        return out
    bases: dict = {}   # shared: two scenarios replacing vivienda need one median
    out["scenarios"] = [project(conn, s, config, start, categories, bases)
                        for s in config.scenarios]
    out["months"] = [r["month"] for r in out["scenarios"][0]["rows"]] if out["scenarios"] else []
    return out
