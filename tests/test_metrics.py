"""The properties the metrics have to hold whatever the data: the breakdown
reconciles with the total, internal movements never reach either side of the
ledger, thin history is flagged rather than guessed, and a real subscription
survives an off-cycle charge from the same merchant."""

import tempfile
import unittest
from pathlib import Path

from finanzas import db, metrics
from finanzas.categories import Category
from finanzas.goals import Goals, Limit

# A taxonomy small enough to reason about, with the two flags exercised.
CATS = {
    "super":  Category("super", "Supermercado", "expense"),
    "subs":   Category("subs", "Suscripciones", "expense", fixed=True),
    "gym":    Category("gym", "Gimnasio", "expense", fixed=True),
    "salidas": Category("salidas", "Salidas", "expense", discretionary=True),
    "nomina": Category("nomina", "Nomina", "income"),
    "interno": Category("interno", "Entre cuentas", "internal"),
}


class MetricsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        self.conn.execute("INSERT INTO imports (id, sha256, filename, bank, account, imported_at, "
                          "n_rows, n_inserted, n_duplicate) VALUES (1,'x','x.xls','bbva','bbva',"
                          "'2026-09-01T00:00:00',0,0,0)")
        self.conn.execute("INSERT INTO raw_rows (id, import_id, position, data) VALUES (1,1,0,'{}')")
        self.n = 0

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add(self, date, amount_cents, description="x", category=None, account="bbva"):
        self.n += 1
        self.conn.execute(
            "INSERT INTO transactions (account, date, date_booked, amount_cents, balance_cents, "
            "occurrence, description, description_norm, raw_row_id, category, category_src) "
            "VALUES (?,?,?,?,?,?,?,?,1,?,'rule:t')",
            (account, date, date, amount_cents, self.n * 1000, self.n,
             description, description, category))


class Coverage(MetricsBase):
    def test_partial_edge_months_are_not_complete(self):
        self.add("2026-06-23", -100)
        self.add("2026-09-20", -100)
        self.assertEqual(metrics.coverage(self.conn), ("2026-06-23", "2026-09-20"))
        self.assertEqual(metrics.complete_months(self.conn), ["2026-07", "2026-08"])

    def test_an_account_missing_its_latest_export_shortens_coverage(self):
        # bbva is exported through September, santander stops in July. August is
        # not a month you can report on: half the spending is not in the file.
        self.add("2026-06-01", -100, account="bbva")
        self.add("2026-09-30", -100, account="bbva")
        self.add("2026-06-01", -100, account="santander")
        self.add("2026-07-31", -100, account="santander")
        self.assertEqual(metrics.complete_months(self.conn), ["2026-06", "2026-07"])


class MonthlySummary(MetricsBase):
    def test_category_totals_sum_to_spend(self):
        self.add("2026-07-02", -5000, category="super")
        self.add("2026-07-03", -1200, category="salidas")
        self.add("2026-07-04", -800)                      # uncategorized counts as spend
        self.add("2026-07-05", 150000, category="nomina")
        s = metrics.monthly_summary(self.conn, "2026-07", CATS)
        self.assertEqual(sum(c["amount_cents"] for c in s["by_category"]), s["spend_cents"])
        self.assertEqual(s["spend_cents"], 7000)
        self.assertEqual(s["income_cents"], 150000)
        self.assertEqual(s["net_cents"], 143000)

    def test_internal_movements_touch_neither_side(self):
        self.add("2026-07-02", -50000, category="interno")
        self.add("2026-07-02", 50000, category="interno", account="santander")
        s = metrics.monthly_summary(self.conn, "2026-07", CATS)
        self.assertEqual((s["income_cents"], s["spend_cents"]), (0, 0))

    def test_a_refund_lowers_the_category_net(self):
        # D13: the refund lands in the category it refunds, so the total is net.
        self.add("2026-07-02", -3000, category="salidas")
        self.add("2026-07-09", 1000, category="salidas")
        s = metrics.monthly_summary(self.conn, "2026-07", CATS)
        self.assertEqual(s["by_category"][0]["amount_cents"], 2000)

    def test_no_baseline_means_no_deviation_rather_than_infinity(self):
        self.add("2026-07-02", -3000, category="super")
        s = metrics.monthly_summary(self.conn, "2026-07", CATS)
        self.assertIsNone(s["by_category"][0]["deviation_pct"])
        self.assertTrue(s["low_confidence"])


class Recurring(MetricsBase):
    def monthly_series(self, desc, cents, days, category="subs"):
        for day in days:
            self.add(f"2026-{day[:2]}-{day[2:]}", -cents, desc, category)

    def test_detects_a_clean_monthly_series(self):
        self.monthly_series("vodafone", 1000, ["0601", "0701", "0801"])
        self.add("2026-08-31", -100)  # so the ledger covers to the end of August
        found = metrics.find_recurring(self.conn, CATS)
        self.assertEqual([f["description_norm"] for f in found], ["vodafone"])
        self.assertEqual(found[0]["cadence"], "monthly")
        self.assertEqual(found[0]["annual_cents"], 12000)

    def test_an_off_cycle_charge_does_not_hide_the_subscription(self):
        # The real case: Anthropic bills 18 EUR on the 23rd and once took 21.78
        # on the 1st. Over all four charges the gaps are 8, 22, 31 days and the
        # series looks irregular; over the amount cluster they are 30 and 31.
        self.monthly_series("anthropic", 1800, ["0623", "0723", "0823"])
        self.add("2026-07-01", -2178, "anthropic", "subs")
        found = metrics.find_recurring(self.conn, CATS)
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["amount_cents"], found[0]["occurrences"],
                          found[0]["skipped"]), (1800, 3, 1))

    def test_rejects_a_frequent_but_irregular_merchant(self):
        for day in ["0702", "0703", "0709", "0711", "0718", "0801", "0815"]:
            self.add(f"2026-{day[:2]}-{day[2:]}", -1500, "mercadona", "super")
        self.assertEqual(metrics.find_recurring(self.conn, CATS), [])

    def test_two_charges_are_not_a_pattern(self):
        self.monthly_series("basic-fit", 2499, ["0708", "0805"])
        self.assertEqual(metrics.find_recurring(self.conn, CATS), [])

    def test_a_series_that_stopped_is_marked_stale(self):
        self.monthly_series("basic-fit", 2499, ["0405", "0505", "0605"])
        self.add("2026-09-20", -100)  # the ledger runs on; the gym does not
        found = metrics.find_recurring(self.conn, CATS)
        self.assertEqual(len(found), 1)
        self.assertFalse(found[0]["active"])


class FixedVariable(MetricsBase):
    def test_the_split_is_exact_and_covers_the_fixed_category(self):
        self.add("2026-07-02", -5000, "mercadona", "super")
        self.add("2026-07-03", -2499, "un gimnasio nuevo", "gym")   # fixed by category
        self.add("2026-07-04", -1200, "cena", "salidas")
        self.add("2026-07-05", 200000, "nomina", "nomina")          # income, not spend
        self.add("2026-07-06", -30000, "traspaso", "interno")       # internal, not spend
        split = metrics.fixed_vs_variable(self.conn, "2026-07", CATS)
        self.assertEqual(split["fixed_cents"], 2499)
        self.assertEqual(split["variable_cents"], 6200)
        self.assertEqual(split["total_cents"],
                         metrics.monthly_summary(self.conn, "2026-07", CATS)["spend_cents"])

    def test_a_stale_series_is_not_counted_as_a_fixed_cost(self):
        # Detected, but cancelled: counting it would overstate every forward number.
        for day in ["0405", "0505", "0605"]:
            self.add(f"2026-{day[:2]}-{day[2:]}", -2499, "gimnasio viejo")
        self.add("2026-09-20", -100)
        self.add("2026-09-21", -1000, "algo")
        split = metrics.fixed_vs_variable(self.conn, "2026-09", CATS)
        self.assertEqual(split["fixed_cents"], 0)


class Rates(MetricsBase):
    def test_burn_rate_takes_the_median_of_complete_months_only(self):
        for month, cents in [("06", 10000), ("07", 20000), ("08", 900000)]:
            self.add(f"2026-{month}-15", -cents, "gasto", "super")
        self.add("2026-06-14", 0, "inicio")   # June starts on the 14th: partial
        self.add("2026-09-01", 0, "fin")      # September stops on the 1st: partial
        burn = metrics.burn_rate(self.conn, CATS)
        self.assertEqual(burn["months"], ["2026-07", "2026-08"])
        self.assertTrue(burn["low_confidence"])
        self.assertEqual(burn["median_cents"], 460000)

    def test_runway_divides_and_can_be_overridden(self):
        self.add("2026-07-01", -100000, "gasto", "super")
        self.add("2026-07-31", -100000, "gasto", "super")
        run = metrics.runway(self.conn, 600000, CATS)
        self.assertEqual(run["liquid_source"], "override")
        self.assertEqual(run["months"], 3.0)

    def test_runway_is_none_when_there_is_nothing_to_divide_by(self):
        self.add("2026-07-01", 100000, "devolucion", "super")   # a refunded month
        self.add("2026-07-31", 0, "x", "super")
        self.assertIsNone(metrics.runway(self.conn, 600000, CATS)["months"])

    def test_savings_rate_is_none_without_income(self):
        self.add("2026-07-02", -5000, "gasto", "super")
        rate = metrics.savings_rate(self.conn, "2026-07", CATS)
        self.assertIsNone(rate["rate"])
        self.assertIsNone(rate["rolling_rate"])

    def test_discretionary_share_uses_the_flag(self):
        self.add("2026-07-02", -7500, "compra", "super")
        self.add("2026-07-03", -2500, "cena", "salidas")
        share = metrics.discretionary_share(self.conn, "2026-07", CATS)
        self.assertEqual(share["discretionary_cents"], 2500)
        self.assertAlmostEqual(share["share"], 0.25)


class Pace(MetricsBase):
    """Month to date against a limit (5.2). The two things that can go wrong here
    are dividing by the wrong number of days and projecting something that does
    not accrue daily."""

    def limits(self, *limits):
        return Goals(tuple(limits))

    def test_projection_scales_by_days_elapsed_in_the_ledger(self):
        # The ledger stops on the 15th, so half a month of spending projects to
        # roughly a full month: 31/15 of what landed.
        self.add("2026-07-01", -10000, category="super")
        self.add("2026-07-15", -5000, category="super")
        pace = metrics.month_to_date(
            self.conn, "2026-07", self.limits(Limit("super", 40000, "2026-01")), CATS)
        self.assertEqual(pace["as_of"], "2026-07-15")
        self.assertEqual(pace["days_elapsed"], 15)
        line = pace["lines"][0]
        self.assertEqual(line["spent_cents"], 15000)
        self.assertEqual(line["projected_cents"], round(15000 * 31 / 15))
        self.assertTrue(line["projected"])
        self.assertFalse(line["over"])
        self.assertFalse(line["over_projected"])

    def test_the_wall_clock_never_enters_it(self):
        # A ledger that stops on the 10th has twenty days of missing data, not
        # twenty days of restraint. The figure must not change with the date the
        # report is run, so it is pinned to the export, not to today.
        self.add("2026-07-01", -20000, category="super")
        self.add("2026-07-10", -10000, category="super")
        pace = metrics.month_to_date(
            self.conn, "2026-07", self.limits(Limit("super", 40000, "2026-01")), CATS)
        self.assertEqual(pace["days_elapsed"], 10)
        self.assertEqual(pace["lines"][0]["projected_cents"], round(30000 * 31 / 10))

    def test_a_complete_month_is_not_projected(self):
        self.add("2026-07-01", -10000, category="super")
        self.add("2026-08-05", -100, category="super")     # carries coverage past July
        pace = metrics.month_to_date(
            self.conn, "2026-07", self.limits(Limit("super", 40000, "2026-01")), CATS)
        self.assertEqual(pace["days_elapsed"], 31)
        self.assertFalse(pace["lines"][0]["projected"])
        self.assertEqual(pace["lines"][0]["projected_cents"], 10000)

    def test_a_fixed_category_is_not_projected(self):
        # The gym charge lands whole on the 2nd. Scaling it by the month would
        # report a 30 EUR commitment as a 465 EUR one.
        self.add("2026-07-02", -3000, category="gym")
        self.add("2026-07-02", -6000, category="super")
        pace = metrics.month_to_date(
            self.conn, "2026-07",
            self.limits(Limit("gym", 4000, "2026-01"), Limit("super", 40000, "2026-01")), CATS)
        gym = next(l for l in pace["lines"] if l["category"] == "gym")
        self.assertEqual(gym["projected_cents"], 3000)
        self.assertFalse(gym["projected"])
        sup = next(l for l in pace["lines"] if l["category"] == "super")
        self.assertTrue(sup["projected"])

    def test_a_limit_on_a_parent_covers_its_children(self):
        cats = dict(CATS)
        cats["salidas-cafes"] = Category("salidas-cafes", "Cafes", "expense",
                                         parent="salidas", discretionary=True)
        self.add("2026-07-02", -2000, category="salidas")
        self.add("2026-07-03", -1000, category="salidas-cafes")
        pace = metrics.month_to_date(
            self.conn, "2026-07", self.limits(Limit("salidas", 10000, "2026-01")), cats)
        self.assertEqual(pace["lines"][0]["spent_cents"], 3000)

    def test_the_limit_in_force_is_the_one_from_that_month(self):
        self.add("2026-07-02", -35000, category="super")
        goals = self.limits(Limit("super", 30000, "2026-01"), Limit("super", 40000, "2026-09"))
        july = metrics.month_to_date(self.conn, "2026-07", goals, CATS)
        self.assertEqual(july["lines"][0]["limit_cents"], 30000)
        self.assertTrue(july["lines"][0]["over"])

    def test_a_category_without_a_limit_is_not_a_line(self):
        self.add("2026-07-02", -9999, category="salidas")
        pace = metrics.month_to_date(
            self.conn, "2026-07", self.limits(Limit("super", 40000, "2026-01")), CATS)
        self.assertEqual([l["category"] for l in pace["lines"]], ["super"])
        self.assertEqual(pace["lines"][0]["spent_cents"], 0)

    def test_no_limits_is_an_empty_report_not_an_error(self):
        self.add("2026-07-02", -9999, category="super")
        pace = metrics.month_to_date(self.conn, "2026-07", Goals(), CATS)
        self.assertEqual(pace["lines"], [])
        self.assertEqual(pace["limit_cents"], 0)

    def test_a_month_the_ledger_has_not_reached_has_no_elapsed_days(self):
        # Dividing by them is the one way this function can raise.
        self.add("2026-07-02", -9999, category="super")
        pace = metrics.month_to_date(
            self.conn, "2026-09", self.limits(Limit("super", 40000, "2026-01")), CATS)
        self.assertEqual(pace["days_elapsed"], 0)
        self.assertEqual(pace["lines"][0]["projected_cents"], 0)


class LiquidOverride(MetricsBase):
    def test_goals_liquid_beats_the_account_sum_and_the_flag_beats_both(self):
        self.add("2026-07-01", -10000, category="super")
        accounts = metrics.runway(self.conn, None, CATS, Goals())
        self.assertEqual(accounts["liquid_source"], "accounts")
        goals = metrics.runway(self.conn, None, CATS, Goals(liquid_cents=480000))
        self.assertEqual((goals["liquid_source"], goals["liquid_cents"]), ("goals.yaml", 480000))
        flag = metrics.runway(self.conn, 100000, CATS, Goals(liquid_cents=480000))
        self.assertEqual((flag["liquid_source"], flag["liquid_cents"]), ("override", 100000))


if __name__ == "__main__":
    unittest.main()


class RecurringIsAboutCosts(MetricsBase):
    def test_a_withholding_in_an_income_category_is_not_a_subscription(self):
        # Real case: "retencion promocion comercial" is a negative amount in an
        # income category (D13), monthly, eight times over. Sign alone calls it a
        # recurring cost; it is money arriving, just less of it.
        for day in ["0403", "0503", "0603"]:
            self.add(f"2026-{day[:2]}-{day[2:]}", -1013, "retencion promocion", "nomina")
        self.add("2026-06-30", -100)
        self.assertEqual(metrics.find_recurring(self.conn, CATS), [])


class BurnWindow(MetricsBase):
    def test_the_burn_window_is_longer_than_the_category_baseline(self):
        # One extreme month must not set the runway. Eleven ordinary months of
        # 1,000 and one of 9,000: a 3 month median would read 1,000 or 9,000
        # depending only on where the outlier fell.
        for i, month in enumerate(["01", "02", "03", "04", "05", "06",
                                   "07", "08", "09", "10", "11", "12"]):
            self.add(f"2025-{month}-15", -900000 if month == "12" else -100000, "gasto", "super")
        self.add("2024-12-31", 0, "inicio")
        self.add("2026-01-01", 0, "fin")
        burn = metrics.burn_rate(self.conn, CATS)
        self.assertEqual(len(burn["months"]), 12)
        self.assertEqual(burn["median_cents"], 100000)
        self.assertFalse(burn["low_confidence"])


class FindTransactions(MetricsBase):
    """The filter both `list` and the web page run on. The property that matters
    is composition: every filter narrows, none of them replaces another."""

    def setUp(self):
        super().setUp()
        self.add("2026-07-01", -1000, "mercadona valencia", "super")
        self.add("2026-07-02", -2000, "cafe bali", "salidas")
        self.add("2026-08-03", -3000, "mercadona centro", "super", account="santander")
        self.add("2026-08-04", -400, "bizum orxata", None)

    def ids(self, **kw):
        return [r["description"] for r in metrics.find_transactions(self.conn, **kw)]

    def test_filters_compose_with_and(self):
        self.assertEqual(self.ids(month="2026-08"), ["bizum orxata", "mercadona centro"])
        self.assertEqual(self.ids(month="2026-08", account="santander"), ["mercadona centro"])
        self.assertEqual(self.ids(month="2026-08", account="bbva"), ["bizum orxata"])
        self.assertEqual(self.ids(month="2026-07", search="mercadona"), ["mercadona valencia"])
        self.assertEqual(self.ids(search="mercadona", category="super", month="2026-08"),
                         ["mercadona centro"])

    def test_no_filters_returns_everything(self):
        self.assertEqual(len(self.ids()), 4)

    def test_uncategorized_sentinel_selects_only_null(self):
        # Not the same as "no category filter", and not expressible as a category
        # id, which is why the sentinel exists.
        self.assertEqual(self.ids(category=metrics.UNCATEGORIZED), ["bizum orxata"])

    def test_a_parent_category_includes_its_children(self):
        # Asking for salidas and getting nothing while salidas-cafes has rows
        # would be a filter lying about what it selected.
        self.conn.execute("UPDATE transactions SET category = 'salidas-cafes' "
                          "WHERE description = 'cafe bali'")
        self.assertEqual(self.ids(category="salidas"), ["cafe bali"])
        self.assertEqual(self.ids(category="salidas-cafes"), ["cafe bali"])
        self.assertEqual(self.ids(category="super"), ["mercadona centro", "mercadona valencia"])

    def test_newest_first_and_limit_keeps_the_newest(self):
        self.assertEqual(self.ids(limit=2), ["bizum orxata", "mercadona centro"])


# A two level taxonomy for the matrix: a parent with children, one of them
# refundable, and an income tree, so parent = sum of children can be checked.
TREE = {
    "compras":       Category("compras", "Compras", "expense"),
    "compras-ropa":  Category("compras-ropa", "Ropa", "expense", "compras"),
    "compras-hogar": Category("compras-hogar", "Hogar", "expense", "compras"),
    "moto":          Category("moto", "Moto", "expense"),
    "super":         Category("super", "Supermercado", "expense"),
    "ingresos":      Category("ingresos", "Ingresos", "income"),
    "ingresos-paro": Category("ingresos-paro", "Paro", "income", "ingresos"),
    "interno":       Category("interno", "Entre cuentas", "internal"),
}


class AnnualMatrix(MetricsBase):
    def months(self, first: str, n: int):
        """n complete months starting at `first`, each with 100 EUR of groceries."""
        out = [first]
        while len(out) < n:
            y, m = int(out[-1][:4]), int(out[-1][5:7])
            out.append(f"{y + (m == 12):04d}-{1 if m == 12 else m + 1:02d}")
        for month in out:
            self.add(f"{month}-01", -10000, "mercadona", "super")
        self.add(f"{out[-1]}-{metrics.month_end(out[-1]).day:02d}", -1, "cierre", "super")
        return out

    def row(self, m, cid):
        return next(r for r in m["rows"] if r["id"] == cid)

    def test_columns_equal_monthly_summary_and_parents_equal_children(self):
        months = self.months("2026-01", 3)
        self.add("2026-01-10", -4000, "zara", "compras-ropa")
        self.add("2026-02-10", -2500, "ikea", "compras-hogar")
        self.add("2026-02-11", -700, "tienda", "compras")        # filed on the parent itself
        self.add("2026-02-12", -900)                            # uncategorized
        self.add("2026-02-13", -50000, "a myinvestor", "interno")
        self.add("2026-03-02", 60000, "sepe", "ingresos-paro")
        m = metrics.annual_matrix(self.conn, None, TREE)
        self.assertEqual(m["months"], months)
        for i, month in enumerate(months):
            s = metrics.monthly_summary(self.conn, month, TREE)
            self.assertEqual(m["totals"]["cells"][i]["cents"], s["spend_cents"])
            self.assertEqual(sum(r["cells"][i]["cents"] for r in m["rows"]), s["spend_cents"])
            self.assertEqual(m["income"]["cells"][i]["cents"], s["income_cents"])
        compras = self.row(m, "compras")
        self.assertEqual([c["cents"] for c in compras["cells"]], [4000, 3200, 0])
        for i in range(3):
            self.assertEqual(sum(c["cells"][i]["cents"] for c in compras["children"]),
                             compras["cells"][i]["cents"])
        # The parent's own transaction becomes a child row, or the sum would be short.
        direct = [c for c in compras["children"] if c.get("direct")]
        self.assertEqual([c["total_cents"] for c in direct], [700])
        self.assertEqual(self.row(m, metrics.UNCATEGORIZED)["total_cents"], 900)
        self.assertEqual(m["income"]["id"], "ingresos")
        self.assertEqual(sum(r["total_cents"] for r in m["rows"]), m["grand_total_cents"])
        # Internal never appears, on either side.
        self.assertNotIn("interno", [r["id"] for r in m["rows"]])
        self.assertEqual(m["income"]["total_cents"], 60000)

    def test_a_refund_month_goes_negative_and_reads_below_the_median(self):
        self.months("2026-01", 3)
        self.add("2026-01-10", -5000, "zara", "compras-ropa")
        self.add("2026-02-10", -5000, "zara", "compras-ropa")
        self.add("2026-03-10", 2000, "zara devolucion", "compras-ropa")  # D13: a net refund
        ropa = self.row(metrics.annual_matrix(self.conn, None, TREE), "compras")
        self.assertEqual([c["cents"] for c in ropa["cells"]], [5000, 5000, -2000])
        self.assertEqual(ropa["total_cents"], 8000)
        self.assertEqual(ropa["median_cents"], 5000)
        self.assertEqual(ropa["months_with_spend"], 3)
        self.assertAlmostEqual(ropa["cells"][2]["deviation"], -1.4)   # (-20 - 50) / 50
        self.assertEqual(ropa["cells"][0]["deviation"], 0)

    def test_a_one_off_has_a_zero_median_and_one_month_with_spend(self):
        self.months("2026-01", 4)
        self.add("2026-02-20", -312900, "taller", "moto")
        moto = self.row(metrics.annual_matrix(self.conn, None, TREE), "moto")
        self.assertEqual(moto["months_with_spend"], 1)
        self.assertEqual(moto["median_cents"], 0)
        self.assertEqual(moto["total_cents"], 312900)
        # Zero months are neutral; the spike is scaled by the floor, not divided by 0.
        devs = [c["deviation"] for c in moto["cells"]]
        self.assertEqual(devs[0], 0)
        self.assertEqual(devs[1], 312900 / metrics.DEVIATION_FLOOR_CENTS)

    def test_a_short_window_shows_what_exists_and_says_so(self):
        self.months("2026-01", 2)
        m = metrics.annual_matrix(self.conn, None, TREE)
        self.assertEqual(m["months"], ["2026-01", "2026-02"])
        self.assertTrue(m["low_confidence"])
        self.assertEqual([r["id"] for r in m["rows"]], ["super"])

    def test_no_complete_month_is_an_empty_matrix_not_an_error(self):
        self.add("2026-01-10", -100, "x", "super")
        m = metrics.annual_matrix(self.conn, None, TREE)
        self.assertEqual((m["months"], m["rows"], m["end_month"]), ([], [], None))

    def test_window_is_twelve_months_and_end_selects_it(self):
        months = self.months("2025-01", 14)
        m = metrics.annual_matrix(self.conn, None, TREE)
        self.assertEqual(m["months"], months[-12:])
        self.assertFalse(m["low_confidence"])
        m = metrics.annual_matrix(self.conn, "2025-06", TREE)
        self.assertEqual(m["months"], months[:6])
        # An end that is not a complete month falls back to the default (D22).
        for bad in ("2031-01", "garbage", "2025-00"):
            self.assertEqual(metrics.annual_matrix(self.conn, bad, TREE)["end_month"], months[-1])
