"""The monthly review: a score out of 10 and the facts worth talking about.

This is the deterministic half of `/asesor`. Claude reads what this module
returns and writes the commentary; it never computes or adjusts the score. Same
split as D7 for categorization: if the model produced the number, August would
score one thing today and another next week, and a score that moves on its own
cannot be compared across months, which is the only thing a score is for.

The score measures what the owner controls, plus one absolute anchor:

  spend          40  the month's spend against its own trailing median
  limits         25  the limits in goals.yaml that were in force that month
  discretionary  15  optional spend against its own trailing median
  runway         20  months of runway at month end, on an absolute scale

Income is deliberately absent. Income is often an event (a severance payment,
a tax refund), not behavior, and a cash-flow component would score a month
well while spend ran 21% over the norm, just because a one-off landed. Runway
is the one place income leaks back in, and it is there on purpose: a score
relative only to your own history is a treadmill that rates a steady
overspender 8/10.

A component that does not apply (no limits in force that month) is dropped and
the others are reweighted, so a month is never marked down for a rule that did
not exist yet. Nothing is exempt: a planned purchase counts in full. The review explains a low month; it does not negotiate it.
"""

import re
import statistics
from pathlib import Path

from finanzas import goals as goals_mod, metrics
from finanzas.categories import Category, load as load_categories
from finanzas.check import walk_chain

# Where `/asesor` writes each month's review, relative to the repo root like every
# other file the app reads. In git, not in the database: the next review reads the
# previous one's commitments, and the database is rebuilt from the inbox (D15).
REVIEWS_DIR = Path("reviews")

WEIGHTS = {"spend": 40, "limits": 25, "discretionary": 15, "runway": 20}

# (ratio at which the component is still 10, ratio at which it reaches 0), linear
# between. Calibrated on the real ledger 2026-01..2026-08, where 9 of 11 months sat
# within about 20% of the median: a normal month should land 7-10, the June and
# motorbike months near 0, and the scale should not saturate on ordinary noise.
SPEND_BAND = (0.85, 1.50)
DISCRETIONARY_BAND = (0.90, 1.60)
LIMIT_BAND = (1.00, 1.50)       # at the limit is fine, 50% over is zero
RUNWAY_MONTHS = (2.0, 6.0)      # 2 months or less is 0, 6 or more is 10

# Discretionary spend is small (a median of 100-180 EUR), so a few dinners swing the
# ratio from 0.5 to 2. The denominator never drops below this, which keeps one
# night out from reading as a collapse in discipline.
DISCRETIONARY_FLOOR_CENTS = 5000

BASELINE_MONTHS = metrics.BURN_WINDOW_MONTHS

# A single charge at least this share of the baseline month is worth naming on its
# own. At today's 1,168 baseline that is 175 EUR: it caught 0 to 3 charges a month
# over the real ledger, and they were the right ones (the motorbike, the big cash
# withdrawals, rent), where a category deviation rule caught 3 to 6 mostly noise.
BIG_CHARGE_SHARE = 0.15

# Category deviations are ranked by euros, never by percent: compras-hogar going
# from 4.60 to 240.96 is +5138% and 236 EUR, and only the second number means
# anything. Below this many euros over the median a category is not worth a line.
DEVIATION_FLOOR_CENTS = 5000
TOP_DEVIATIONS = 3

# Above this share of spend going to opaque categories (cash, misc) plus
# uncategorized, the score is marked low confidence: the review would be judging the
# half of the month it can see.
OPAQUE_WARN_SHARE = 0.20


def _band(ratio: float, lo: float, hi: float) -> float:
    """10 at or below lo, 0 at or above hi, linear between. Higher ratio is worse."""
    if ratio <= lo:
        return 10.0
    if ratio >= hi:
        return 0.0
    return 10.0 * (hi - ratio) / (hi - lo)


def _liquid_at(chains: dict[str, list], day: str) -> int:
    """Sum of each account's balance at the end of `day`, following the chain order
    the same way liquid_balance does. An account with nothing before that day (bbva
    before it was opened) contributes zero."""
    total = 0
    for ordered in chains.values():
        upto = [r for r in ordered if r["date_booked"] <= day]
        if upto:
            total += upto[-1]["balance_cents"]
    return total


def month_review(conn, month: str, categories: dict[str, Category] | None = None,
                 goals: goals_mod.Goals | None = None) -> dict:
    categories = categories or load_categories()
    goals = goals if goals is not None else goals_mod.load(categories=categories)

    done = metrics.complete_months(conn)
    prior = [m for m in done if m < month][-BASELINE_MONTHS:]
    split = {m: metrics._split(metrics._rows_in(conn, m), categories) for m in prior + [month]}
    this = split[month]
    spend = this["spend_cents"]

    chains, n_breaks = {}, 0
    for (account,) in conn.execute("SELECT DISTINCT account FROM transactions ORDER BY 1"):
        chains[account], breaks = walk_chain(conn, account)
        n_breaks += len(breaks)

    # Reasons not to score at all. Each one is a data problem, not a bad month, and
    # a number printed on top of it would be read as a verdict anyway.
    blocked = []
    if month not in done:
        blocked.append(f"{month} is not a complete month in the ledger")
    if len(prior) < metrics.MIN_BASELINE_MONTHS:
        blocked.append(f"only {len(prior)} complete month(s) before {month}, "
                       f"need {metrics.MIN_BASELINE_MONTHS}")
    if n_breaks:
        blocked.append(f"{n_breaks} balance chain break(s); run `check`")

    def disc(m):
        return sum(v for c, v in split[m]["by_category"].items()
                   if c and categories[c].discretionary)

    base_spend = round(statistics.median(split[m]["spend_cents"] for m in prior)) if prior else None
    base_disc = round(statistics.median(disc(m) for m in prior)) if prior else None

    components = {}
    if not blocked and base_spend and base_spend > 0:
        ratio = spend / base_spend
        components["spend"] = {"value": ratio, "baseline_cents": base_spend,
                               "points": _band(ratio, *SPEND_BAND)}

        ratio = disc(month) / max(base_disc, DISCRETIONARY_FLOOR_CENTS)
        components["discretionary"] = {"value": ratio, "spent_cents": disc(month),
                                       "baseline_cents": base_disc,
                                       "points": _band(ratio, *DISCRETIONARY_BAND)}

        pace = metrics.month_to_date(conn, month, goals, categories)
        if pace["lines"]:
            per_limit = [_band(line["spent_cents"] / line["limit_cents"], *LIMIT_BAND)
                         for line in pace["lines"]]
            components["limits"] = {
                "value": sum(not line["over"] for line in pace["lines"]) / len(pace["lines"]),
                "lines": [{k: line[k] for k in ("category", "limit_cents", "spent_cents", "over")}
                          for line in pace["lines"]],
                "points": sum(per_limit) / len(per_limit)}

        liquid = _liquid_at(chains, metrics.month_end(month).isoformat())
        months_left = liquid / base_spend
        components["runway"] = {"value": months_left, "liquid_cents": liquid,
                                "points": _band(-months_left, -RUNWAY_MONTHS[1], -RUNWAY_MONTHS[0])}

    applied = sum(WEIGHTS[k] for k in components)
    for k, c in components.items():
        c["weight"] = WEIGHTS[k] / applied
    score = round(sum(c["points"] * c["weight"] for c in components.values()), 1) if components else None

    opaque = sum(v for c, v in this["by_category"].items() if c is None or categories[c].opaque)
    # Clamped at zero: a month where a friend paid back a Bizum in misc has negative
    # opaque spend, which is money arriving, not visibility gained.
    opaque_share = max(0.0, opaque / spend) if spend > 0 else 0.0

    big = []
    if base_spend:
        for r in metrics._rows_in(conn, month):
            cid = r["category"]
            if (cid is None or categories[cid].type == "expense") \
                    and -r["amount_cents"] >= BIG_CHARGE_SHARE * base_spend:
                big.append({"id": r["id"], "date": r["date"], "amount_cents": -r["amount_cents"],
                            "category": cid, "description": r["description_norm"]})
        big.sort(key=lambda b: -b["amount_cents"])

    # No baseline, no deviations: against a median of nothing every category is
    # "over", and the first month of the ledger would list its groceries as news.
    deviations = []
    for cid, amount in (this["by_category"].items() if prior else ()):
        median = round(statistics.median(split[m]["by_category"].get(cid, 0) for m in prior))
        if amount - median >= DEVIATION_FLOOR_CENTS:
            deviations.append({"category": cid, "amount_cents": amount, "median_cents": median,
                               "delta_cents": amount - median})
    deviations.sort(key=lambda d: -d["delta_cents"])

    # Recurring charges that billed something other than their usual amount this
    # month: a price rise, a plan change or an overage. find_recurring keeps those
    # out of the series (they are its "skipped" charges), so they are looked up here.
    series = {r["description_norm"]: r for r in metrics.find_recurring(conn, categories)}
    price_changes = []
    for r in metrics._rows_in(conn, month):
        s = series.get(r["description_norm"])
        if s and r["amount_cents"] < 0 and \
                abs(-r["amount_cents"] - s["amount_cents"]) > metrics.AMOUNT_TOLERANCE * s["amount_cents"]:
            price_changes.append({"description": r["description_norm"], "date": r["date"],
                                  "amount_cents": -r["amount_cents"], "usual_cents": s["amount_cents"]})

    return {
        "month": month,
        "score": score,
        "blocked": blocked,
        "low_confidence": bool(score is not None and opaque_share > OPAQUE_WARN_SHARE),
        "components": components,
        "baseline_months": prior,
        "income_cents": this["income_cents"],
        "spend_cents": spend,
        "net_cents": this["income_cents"] - spend,
        "opaque_cents": opaque,
        "opaque_share": opaque_share,
        "big_charges": big,
        "deviations": deviations[:TOP_DEVIATIONS],
        "price_changes": price_changes,
        "recurring_live": [{k: r[k] for k in ("description_norm", "category", "amount_cents",
                                              "annual_cents", "last_seen")}
                           for r in series.values() if r["active"]],
    }


def latest_complete(conn) -> str | None:
    """"Last month" means the last month the ledger covers end to end, not the
    calendar's last month: on 22/09 with exports up to 20/09 that is August."""
    done = metrics.complete_months(conn)
    return done[-1] if done else None


def written(month: str) -> str | None:
    """The review `/asesor` wrote for this month, as Markdown, or None if it has not
    been written yet. The app only reads it; the text is Claude's, the score in it
    was copied from month_review."""
    path = REVIEWS_DIR / f"{month}.md"
    return path.read_text(encoding="utf-8") if path.exists() else None


def written_score(text: str) -> float | None:
    """The score a written review states in its title (`# Review 2026-08 · score
    4.7/10`). Compared with month_review's on the Revisiones page: the two differ
    when the ledger changed after the review was written (new rules, a pinned
    category, a late export), and then the text is judging a month that no longer
    looks like that."""
    m = re.search(r"score\s+(\d+(?:\.\d)?)\s*/\s*10", text.split("\n", 1)[0])
    return float(m.group(1)) if m else None
