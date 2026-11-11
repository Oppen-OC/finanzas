"""The monthly review's score has to be a function of the ledger and nothing else:
the same month scores the same every time, a month the ledger does not fully cover
is not scored, a limit that did not exist yet does not count against a month, and
the euro floors keep small categories from reading as anomalies."""

import tempfile
import unittest
from pathlib import Path

from finanzas import db, review
from finanzas.categories import Category
from finanzas.goals import Goals, Limit

CATS = {
    "super": Category("super", "Supermercado", "expense"),
    "salidas": Category("salidas", "Salidas", "expense", discretionary=True),
    "cash": Category("cash", "Efectivo", "expense", opaque=True),
    "moto": Category("moto", "Moto", "expense"),
    "nomina": Category("nomina", "Nomina", "income"),
}


class ReviewBase(unittest.TestCase):
    """Rows go in date order with a consistent running balance, because the review
    refuses to score a ledger whose balance chain breaks."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        self.conn.execute("INSERT INTO imports (id, sha256, filename, bank, account, imported_at, "
                          "n_rows, n_inserted, n_duplicate) VALUES (1,'x','x.xls','bbva','bbva',"
                          "'2026-09-01T00:00:00',0,0,0)")
        self.conn.execute("INSERT INTO raw_rows (id, import_id, position, data) VALUES (1,1,0,'{}')")
        self.balance, self.n = 0, 0

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add(self, date, amount_cents, category, description=None):
        self.n += 1
        self.balance += amount_cents
        self.conn.execute(
            "INSERT INTO transactions (account, date, date_booked, amount_cents, balance_cents, "
            "occurrence, description, description_norm, raw_row_id, category, category_src) "
            "VALUES ('bbva',?,?,?,?,1,?,?,1,?,'rule:t')",
            (date, date, amount_cents, self.balance, description or f"tx{self.n}",
             description or f"tx{self.n}", category))

    def month(self, m, super_c=100000, salidas_c=10000, income_c=0, extra=()):
        """One ordinary month: income on the 1st, groceries spread over eight trips so
        no single one is a big charge, a night out, anything in `extra` on the 20th,
        and a coffee on the last day so the month is covered."""
        if income_c:
            self.add(f"{m}-01", income_c, "nomina")
        for day in range(2, 10):
            self.add(f"{m}-{day:02d}", -super_c // 8, "super")
        self.add(f"{m}-12", -salidas_c, "salidas")
        for cat, amount in extra:
            self.add(f"{m}-20", -amount, cat)
        self.add(f"{m}-28", -100, "super")

    def ledger(self, months=("2026-01", "2026-02", "2026-03", "2026-04"), **last):
        self.add("2025-12-31", 2_000_000, "nomina")   # 20,000 EUR to start: runway is not the question
        for m in months[:-1]:
            self.month(m)
        self.month(months[-1], **last)
        self.add("2026-05-02", -100, "super")         # so the last month is complete

    def review(self, month="2026-04", goals=Goals()):
        return review.month_review(self.conn, month, CATS, goals)


class Scoring(ReviewBase):
    def test_same_month_same_score(self):
        self.ledger()
        self.assertEqual(self.review(), self.review())

    def test_an_ordinary_month_with_a_long_runway_scores_high(self):
        self.ledger()
        r = self.review()
        # Exactly at the median: spend sits inside its band but not at the top of it.
        self.assertGreater(r["score"], 8)
        self.assertEqual(set(r["components"]), {"spend", "discretionary", "runway"})

    def test_components_are_bounded_and_weights_sum_to_one(self):
        self.ledger(extra=[("moto", 500000)])
        r = self.review()
        for c in r["components"].values():
            self.assertTrue(0 <= c["points"] <= 10)
        self.assertAlmostEqual(sum(c["weight"] for c in r["components"].values()), 1.0)
        self.assertEqual(r["components"]["spend"]["points"], 0.0)

    def test_a_limit_counts_only_from_its_since_month(self):
        self.ledger(super_c=200000)
        later = Goals(limits=(Limit("super", 150000, "2026-05"),))
        self.assertNotIn("limits", self.review(goals=later)["components"])
        now = Goals(limits=(Limit("super", 150000, "2026-04"),))
        limits = self.review(goals=now)["components"]["limits"]
        self.assertEqual(limits["value"], 0.0)       # the one limit was broken
        self.assertLess(limits["points"], 10)


class NotScored(ReviewBase):
    def test_partial_month_is_not_scored(self):
        self.ledger()
        r = self.review("2026-05")
        self.assertIsNone(r["score"])
        self.assertTrue(any("not a complete month" in b for b in r["blocked"]))

    def test_thin_history_is_not_scored(self):
        self.ledger(months=("2026-01", "2026-02", "2026-03"))
        r = self.review("2026-03")
        self.assertIsNone(r["score"])

    def test_a_chain_break_blocks_the_score(self):
        self.ledger()
        self.conn.execute("UPDATE transactions SET balance_cents = balance_cents + 1 "
                          "WHERE date >= '2026-03-01'")
        self.assertIsNone(self.review()["score"])


class TalkingPoints(ReviewBase):
    def test_a_big_charge_is_named_and_a_small_spike_is_not(self):
        # Salidas going from 100 to 140 EUR is +40%, and 40 EUR: below the floor.
        self.ledger(salidas_c=14000, extra=[("moto", 300000)])
        r = self.review()
        self.assertEqual([b["category"] for b in r["big_charges"]], ["moto"])
        self.assertEqual([d["category"] for d in r["deviations"]], ["moto"])

    def test_cash_heavy_month_is_low_confidence(self):
        self.ledger(extra=[("cash", 60000)])
        r = self.review()
        self.assertIsNotNone(r["score"])
        self.assertTrue(r["low_confidence"])
        self.assertGreater(r["opaque_share"], review.OPAQUE_WARN_SHARE)


if __name__ == "__main__":
    unittest.main()
