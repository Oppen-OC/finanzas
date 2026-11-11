"""The projection is arithmetic over stated events, so the tests pin the
arithmetic: with no events the balance falls by exactly the burn each month,
events apply on their inclusive edges and nowhere else, `replaces` takes out
exactly the category it names and only while it is active, and the loader
refuses a malformed file by naming the scenario and the event."""

import statistics
import tempfile
import textwrap
import unittest
from pathlib import Path

from finanzas import db, metrics, projection, scenarios
from finanzas.categories import Category
from finanzas.goals import Goals
from finanzas.scenarios import Config, Event, Scenario

CATS = {
    "super": Category("super", "Supermercado", "expense"),
    "vivienda": Category("vivienda", "Vivienda", "expense", fixed=True),
    "vivienda-alquiler": Category("vivienda-alquiler", "Alquiler", "expense", "vivienda", fixed=True),
    "nomina": Category("nomina", "Nomina", "income"),
}

# Six complete months chosen so that the two readings of `replaces` disagree:
# spend is 1300, 1200, 800, 1300, 900, 1400 (median 1250), housing 300, 0, 0,
# 300, 0, 300 (median 150), and spend without housing is 1000, 1200, 800, 1000,
# 900, 1100 (median 1000). burn - median(housing) would say 1100.
SUPER = [1000_00, 1200_00, 800_00, 1000_00, 900_00, 1100_00]
HOUSING = [300_00, 0, 0, 300_00, 0, 300_00]
MONTHS = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]
BURN = 1250_00
BURN_WITHOUT_HOUSING = 1000_00
LIQUID = 10_000_00


class Base(unittest.TestCase):
    end = "2026-06-30"   # on a month end: no stub row

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        self.conn.execute("INSERT INTO imports (id, sha256, filename, bank, account, imported_at, "
                          "n_rows, n_inserted, n_duplicate) VALUES (1,'x','x.xls','bbva','bbva',"
                          "'2026-09-01T00:00:00',0,0,0)")
        self.conn.execute("INSERT INTO raw_rows (id, import_id, position, data) VALUES (1,1,0,'{}')")
        self.n = 0
        for i, m in enumerate(MONTHS):
            self.add(f"{m}-01", -SUPER[i], "super")
            if HOUSING[i]:
                # Half on the parent, half on a child: `replaces: vivienda` has
                # to take out both (D23), or the child leaks into the baseline.
                self.add(f"{m}-02", -HOUSING[i] // 2, "vivienda")
                self.add(f"{m}-03", -HOUSING[i] // 2, "vivienda-alquiler")
            self.add(f"{m}-05", 2000_00, "nomina")   # income never enters the base
        self.add(self.end, -1, "super")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add(self, date, amount, category):
        self.n += 1
        self.conn.execute(
            "INSERT INTO transactions (account, date, date_booked, amount_cents, balance_cents, "
            "occurrence, description, description_norm, raw_row_id, category, category_src) "
            "VALUES ('bbva',?,?,?,?,?,'x','x',1,?,'rule:t')",
            (date, date, amount, self.n * 1000, self.n, category))

    def run_one(self, events=(), horizon=6, floor=0, liquid=LIQUID, goals=None):
        cfg = Config(horizon, floor, (Scenario("s", "S", tuple(events)),))
        out = projection.project_all(self.conn, cfg, CATS, goals or Goals(), liquid)
        return out, out["scenarios"][0]


class Baseline(Base):
    def test_fixture_burn_is_what_the_comment_says(self):
        self.assertEqual(metrics.burn_rate(self.conn, CATS)["median_cents"], BURN)

    def test_no_events_falls_by_exactly_the_burn_each_month(self):
        out, s = self.run_one(horizon=5)
        self.assertIsNone(out["start"]["stub_month"])
        self.assertEqual([r["month"] for r in s["rows"]],
                         ["2026-07", "2026-08", "2026-09", "2026-10", "2026-11"])
        for n, r in enumerate(s["rows"], start=1):
            self.assertEqual(r["closing_cents"], LIQUID - n * BURN)
            self.assertEqual(r["income_cents"], 0)
        self.assertEqual(s["final_cents"], LIQUID - 5 * BURN)

    def test_every_row_chains_from_the_previous_one(self):
        _, s = self.run_one([Event("x", 300_00, True, "2026-08", "2026-09")])
        for a, b in zip(s["rows"], s["rows"][1:]):
            self.assertEqual(a["closing_cents"], b["opening_cents"])
        for r in s["rows"]:
            self.assertEqual(r["closing_cents"],
                             r["opening_cents"] + r["income_cents"] - r["outflow_cents"])

    def test_low_confidence_is_inherited_from_burn_rate(self):
        _, s = self.run_one()
        self.assertFalse(s["low_confidence"])
        self.conn.execute("DELETE FROM transactions WHERE substr(date, 1, 7) < '2026-05'")
        _, s = self.run_one()
        self.assertTrue(s["low_confidence"])

    def test_liquid_flag_then_goals_then_accounts(self):
        out, _ = self.run_one(liquid=123_00, goals=Goals(liquid_cents=456_00))
        self.assertEqual((out["start"]["liquid_cents"], out["start"]["liquid_source"]),
                         (123_00, "override"))
        out, _ = self.run_one(liquid=None, goals=Goals(liquid_cents=456_00))
        self.assertEqual((out["start"]["liquid_cents"], out["start"]["liquid_source"]),
                         (456_00, "goals.yaml"))
        out, _ = self.run_one(liquid=None, goals=Goals())
        self.assertEqual(out["start"]["liquid_source"], "accounts")

    def test_nothing_to_project_from_an_empty_ledger(self):
        self.conn.execute("DELETE FROM transactions")
        out, = [projection.project_all(self.conn, Config(3, 0, (Scenario("s", "S", ()),)),
                                       CATS, Goals(), LIQUID)]
        self.assertIsNone(out["start"])
        self.assertEqual(out["scenarios"], [])


class Events(Base):
    def by_month(self, s):
        return {r["month"]: r for r in s["rows"]}

    def test_from_and_until_are_inclusive_at_both_edges(self):
        _, s = self.run_one([Event("paro", 675_00, True, "2026-08", "2026-10")])
        income = {m: r["income_cents"] for m, r in self.by_month(s).items()}
        self.assertEqual(income, {"2026-07": 0, "2026-08": 675_00, "2026-09": 675_00,
                                  "2026-10": 675_00, "2026-11": 0, "2026-12": 0})

    def test_open_ends_run_from_the_first_month_and_to_the_last(self):
        _, s = self.run_one([Event("a", -10_00, True, None, "2026-08"),
                             Event("b", -20_00, True, "2026-11", None)])
        out = [r["outflow_cents"] - BURN for r in s["rows"]]
        self.assertEqual(out, [10_00, 10_00, 0, 0, 20_00, 20_00])

    def test_a_one_off_lands_in_exactly_one_month(self):
        _, s = self.run_one([Event("matricula", -1200_00, False, "2026-09", "2026-09")])
        extra = [r["outflow_cents"] - BURN for r in s["rows"]]
        self.assertEqual(extra, [0, 0, 1200_00, 0, 0, 0])
        self.assertEqual(s["final_cents"], LIQUID - 6 * BURN - 1200_00)

    def test_replaces_removes_exactly_that_category_and_only_while_active(self):
        rent = Event("alquiler", -450_00, True, "2026-08", "2026-10", "vivienda")
        _, s = self.run_one([rent])
        bases = {m: r["base_cents"] for m, r in self.by_month(s).items()}
        self.assertEqual(bases, {"2026-07": BURN, "2026-08": BURN_WITHOUT_HOUSING,
                                 "2026-09": BURN_WITHOUT_HOUSING, "2026-10": BURN_WITHOUT_HOUSING,
                                 "2026-11": BURN, "2026-12": BURN})
        self.assertEqual(self.by_month(s)["2026-09"]["outflow_cents"],
                         BURN_WITHOUT_HOUSING + 450_00)
        self.assertEqual(self.by_month(s)["2026-09"]["replaced"], ["vivienda"])

    def test_replaces_is_the_median_without_the_category_not_burn_minus_its_median(self):
        # The two readings disagree on this fixture by 100 EUR. Pinned so that a
        # later "simplification" to burn - median(category) fails here.
        self.assertEqual(round(statistics.median(SUPER)), BURN_WITHOUT_HOUSING)
        self.assertNotEqual(BURN - round(statistics.median(HOUSING)), BURN_WITHOUT_HOUSING)
        self.assertEqual(metrics.burn_rate(self.conn, CATS, frozenset({"vivienda"}))["median_cents"],
                         BURN_WITHOUT_HOUSING)

    def test_replacing_only_the_child_leaves_the_parent_in(self):
        _, s = self.run_one([Event("a", -1, True, None, None, "vivienda-alquiler")])
        # housing without the child: 150, 0, 0, 150, 0, 150 -> spend 1150, 1200,
        # 800, 1150, 900, 1250 -> median 1150
        self.assertEqual(s["rows"][0]["base_cents"], 1150_00)


class Crossings(Base):
    def test_floor_and_zero_are_the_first_months_closing_below_them(self):
        # 10,000 - 1,250 n: 8,750 7,500 6,250 5,000 3,750 2,500 1,250 0 -1,250
        _, s = self.run_one(horizon=9, floor=3000_00)
        self.assertEqual(s["floor_month"], "2026-12")   # closes at 2,500
        self.assertEqual(s["zero_month"], "2027-03")                    # 0 is not below zero
        self.assertEqual(s["min_cents"], -1250_00)

    def test_not_within_horizon_is_none(self):
        _, s = self.run_one([Event("nomina", 2000_00, True, None, None)], floor=1500_00)
        self.assertIsNone(s["floor_month"])
        self.assertIsNone(s["zero_month"])


class BreakEven(Base):
    def test_gap_is_steady_outflow_minus_steady_recurring_income(self):
        events = [Event("alquiler", -450_00, True, "2026-08", None, "vivienda"),
                  Event("paro", 675_00, True, None, "2026-09"),          # ends: not steady
                  Event("gym", -40_00, True, None, "2027-06"),           # ends: not steady
                  Event("clases", 200_00, True, "2027-01", None),        # steady income
                  Event("matricula", -900_00, False, "2026-09", "2026-09")]  # one-off
        _, s = self.run_one(events)
        steady_out = BURN_WITHOUT_HOUSING + 450_00
        self.assertEqual(s["required_income_cents"], steady_out)
        self.assertEqual(s["steady_income_cents"], 200_00)
        self.assertEqual(s["gap_cents"], steady_out - 200_00)

    def test_no_events_breaks_even_at_the_burn(self):
        _, s = self.run_one()
        self.assertEqual(s["required_income_cents"], BURN)
        self.assertEqual(s["gap_cents"], BURN)

    def test_a_surplus_is_a_negative_gap(self):
        _, s = self.run_one([Event("nomina", 2000_00, True, None, None)])
        self.assertEqual(s["gap_cents"], BURN - 2000_00)


class Stub(Base):
    end = "2026-07-20"   # ledger ends mid month: July 21-31 is the stub

    def test_the_rest_of_the_ledgers_month_is_prorated_base_with_no_events(self):
        out, s = self.run_one([Event("paro", 675_00, True, None, None),
                               Event("once", -99_00, False, "2026-07", "2026-07")], horizon=2)
        st = out["start"]
        self.assertEqual((st["stub_month"], st["stub_days_left"], st["stub_days_in_month"]),
                         ("2026-07", 11, 31))
        stub = s["rows"][0]
        self.assertTrue(stub["partial"])
        # 1,250 * 11 / 31 = 443.548..., half up to 443.55
        self.assertEqual(stub["outflow_cents"], 443_55)
        self.assertEqual(stub["income_cents"], 0)
        self.assertEqual(stub["events"], [])
        self.assertEqual([r["month"] for r in s["rows"]], ["2026-07", "2026-08", "2026-09"])
        self.assertEqual(s["rows"][1]["income_cents"], 675_00)
        # A one-off dated in the stub month is not applied; it is reported.
        self.assertEqual(s["skipped"], [{"label": "once", "why": "past", "month": "2026-07"}])

    def test_an_event_starting_after_the_horizon_is_reported(self):
        _, s = self.run_one([Event("tarde", 1, True, "2030-01", None)], horizon=2)
        self.assertEqual(s["skipped"][0]["why"], "beyond_horizon")


class Loader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "scenarios.yaml"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, text):
        self.path.write_text(textwrap.dedent(text), encoding="utf-8")
        return scenarios.load(self.path, CATS)

    def refuses(self, text, *fragments):
        with self.assertRaises(ValueError) as cm:
            self.load(text)
        for f in fragments:
            self.assertIn(f, str(cm.exception))

    def test_missing_file_is_none_not_an_error(self):
        self.assertIsNone(scenarios.load(self.path, CATS))

    def test_a_valid_file_loads_to_cents(self):
        cfg = self.load("""
            horizon_months: 12
            min_balance_eur: 1500
            scenarios:
              - id: a
                name: A
                events:
                  - {label: Alquiler, monthly_eur: -450.5, from: 2026-11, replaces: vivienda}
                  - {label: Matricula, eur: -1200, month: 2026-10}
            """)
        self.assertEqual((cfg.horizon_months, cfg.floor_cents), (12, 1500_00))
        rent, once = cfg.scenarios[0].events
        self.assertEqual((rent.cents, rent.recurring, rent.start, rent.end, rent.replaces),
                         (-450_50, True, "2026-11", None, "vivienda"))
        self.assertEqual((once.cents, once.recurring, once.start, once.end),
                         (-1200_00, False, "2026-10", "2026-10"))

    def test_bad_month_format(self):
        self.refuses("""
            scenarios:
              - id: a
                events: [{label: X, monthly_eur: 1, from: 2026-13}]
            """, "'a'", "'X'", "from must be YYYY-MM")
        self.refuses("""
            scenarios:
              - id: a
                events: [{label: X, eur: 1, month: 2026-10-01}]
            """, "'X'", "month must be YYYY-MM")

    def test_until_before_from(self):
        self.refuses("""
            scenarios:
              - id: a
                events: [{label: Paro, monthly_eur: 1, from: 2027-02, until: 2026-12}]
            """, "'a'", "'Paro'", "before from")

    def test_both_or_neither_amount(self):
        self.refuses("""
            scenarios:
              - id: a
                events: [{label: X, monthly_eur: 1, eur: 1, month: 2026-10}]
            """, "'X'", "exactly one")
        self.refuses("""
            scenarios:
              - id: a
                events: [{label: X, from: 2026-10}]
            """, "exactly one")

    def test_duplicate_scenario_ids(self):
        self.refuses("""
            scenarios:
              - {id: a, events: []}
              - {id: a, events: []}
            """, "'a'", "duplicate")

    def test_unknown_category_in_replaces_fails_at_load(self):
        self.refuses("""
            scenarios:
              - id: a
                events: [{label: X, monthly_eur: -1, replaces: viviend}]
            """, "'X'", "unknown category 'viviend'")

    def test_replaces_only_on_recurring_expense(self):
        self.refuses("""
            scenarios:
              - id: a
                events: [{label: X, eur: -1, month: 2026-10, replaces: vivienda}]
            """, "one-off")
        self.refuses("""
            scenarios:
              - id: a
                events: [{label: X, monthly_eur: -1, replaces: nomina}]
            """, "not an expense")

    def test_unknown_keys_are_refused_not_ignored(self):
        # `untill` silently ignored would be an event that never ends.
        self.refuses("""
            scenarios:
              - id: a
                events: [{label: X, monthly_eur: 1, untill: 2026-12}]
            """, "unknown keys", "untill")

    def test_one_off_needs_a_month_and_recurring_refuses_one(self):
        self.refuses("""
            scenarios:
              - id: a
                events: [{label: X, eur: 1}]
            """, "needs `month`")
        self.refuses("""
            scenarios:
              - id: a
                events: [{label: X, monthly_eur: 1, month: 2026-10}]
            """, "not `month`")

    def test_the_starter_file_in_the_repo_loads(self):
        # Against the real taxonomy, since it names real category ids.
        self.assertIsNotNone(scenarios.load(Path("scenarios.yaml")))


if __name__ == "__main__":
    unittest.main()
