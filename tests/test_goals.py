"""goals.yaml has to fail at load time like every other file in the project, and
a limit has to keep its history: changing it today must not rewrite the verdict
on last month."""

import tempfile
import unittest
from pathlib import Path

from finanzas import goals
from finanzas.categories import Category

CATS = {
    "super": Category("super", "Supermercado", "expense"),
    "salidas": Category("salidas", "Salidas", "expense", discretionary=True),
}


class GoalsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "goals.yaml"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text: str) -> goals.Goals:
        self.path.write_text(text, encoding="utf-8")
        return goals.load(self.path, CATS)


class Load(GoalsBase):
    def test_absent_file_is_the_shipping_state(self):
        self.assertEqual(goals.load(self.path, CATS), goals.Goals())

    def test_real_file_loads_if_it_exists(self):
        # Not a fixture: the repo's own goals.yaml must stay loadable, and every
        # category it names must still exist in categories.yaml.
        if Path("goals.yaml").exists():
            goals.load()

    def test_euros_become_cents(self):
        g = self.write("limits:\n  - {category: super, monthly_eur: 350.5, since: 2026-09}\n")
        self.assertEqual(g.limits[0].monthly_cents, 35050)

    def test_fraction_of_a_cent_is_refused(self):
        with self.assertRaises(ValueError):
            self.write("limits:\n  - {category: super, monthly_eur: 350.555, since: 2026-09}\n")

    def test_unknown_category_fails_at_load(self):
        with self.assertRaises(ValueError):
            self.write("limits:\n  - {category: supermecado, monthly_eur: 350, since: 2026-09}\n")

    def test_bad_since(self):
        for since in ("2026-13", "2026", "septiembre"):
            with self.assertRaises(ValueError):
                self.write(f"limits:\n  - {{category: super, monthly_eur: 350, since: {since}}}\n")

    def test_two_limits_starting_the_same_month(self):
        with self.assertRaises(ValueError):
            self.write("limits:\n  - {category: super, monthly_eur: 350, since: 2026-09}\n"
                       "  - {category: super, monthly_eur: 400, since: 2026-09}\n")

    def test_zero_is_not_a_limit(self):
        # Zero would silently mean "over budget on the first coffee", which is
        # never what someone typing 0 into a form meant.
        with self.assertRaises(ValueError):
            self.write("limits:\n  - {category: super, monthly_eur: 0, since: 2026-09}\n")

    def test_unknown_keys(self):
        with self.assertRaises(ValueError):
            self.write("limits:\n  - {category: super, weekly_eur: 80, since: 2026-09}\n")
        with self.assertRaises(ValueError):
            self.write("settings:\n  liquid_euros: 3000\n")


class InForce(GoalsBase):
    def setUp(self):
        super().setUp()
        self.g = self.write(
            "limits:\n"
            "  - {category: super, monthly_eur: 300, since: 2026-01}\n"
            "  - {category: super, monthly_eur: 400, since: 2026-09}\n"
            "  - {category: salidas, monthly_eur: 120, since: 2026-09}\n")

    def test_the_latest_entry_at_or_before_the_month_wins(self):
        self.assertEqual(self.g.at("2026-08")["super"].monthly_cents, 30000)
        self.assertEqual(self.g.at("2026-09")["super"].monthly_cents, 40000)
        self.assertEqual(self.g.at("2026-12")["super"].monthly_cents, 40000)

    def test_a_month_before_any_entry_has_no_limit(self):
        self.assertEqual(self.g.at("2025-12"), {})
        self.assertNotIn("salidas", self.g.at("2026-08"))

    def test_a_lifted_limit_leaves_the_category_out(self):
        g = goals.set_limit("super", None, "2026-10", path=self.path, categories=CATS)
        self.assertEqual(g.at("2026-09")["super"].monthly_cents, 40000)
        self.assertNotIn("super", g.at("2026-10"))


class Writing(GoalsBase):
    def test_set_then_load_round_trips(self):
        goals.set_limit("super", 35000, "2026-09", note="prueba", path=self.path, categories=CATS)
        g = goals.load(self.path, CATS)
        self.assertEqual(g.at("2026-09")["super"].monthly_cents, 35000)
        self.assertEqual(g.history("super")[0].note, "prueba")

    def test_setting_the_same_month_twice_replaces_rather_than_stacks(self):
        goals.set_limit("super", 35000, "2026-09", path=self.path, categories=CATS)
        g = goals.set_limit("super", 40000, "2026-09", path=self.path, categories=CATS)
        self.assertEqual(len(g.history("super")), 1)
        self.assertEqual(g.at("2026-09")["super"].monthly_cents, 40000)

    def test_setting_a_later_month_keeps_the_earlier_one(self):
        goals.set_limit("super", 35000, "2026-01", path=self.path, categories=CATS)
        g = goals.set_limit("super", 40000, "2026-09", path=self.path, categories=CATS)
        self.assertEqual([l.monthly_cents for l in g.history("super")], [35000, 40000])

    def test_lifting_a_limit_that_never_existed_writes_nothing(self):
        # A category with no limit is the default. Recording that it has no
        # limit would be a file entry that says nothing.
        g = goals.unset_limit("super", "2026-09", path=self.path, categories=CATS)
        self.assertEqual(g.limits, ())

    def test_unknown_category_cannot_be_written(self):
        with self.assertRaises(ValueError):
            goals.set_limit("supermecado", 35000, "2026-09", path=self.path, categories=CATS)

    def test_liquid_override_survives_a_limit_edit(self):
        goals.set_liquid(480000, path=self.path, categories=CATS)
        g = goals.set_limit("super", 35000, "2026-09", path=self.path, categories=CATS)
        self.assertEqual(g.liquid_cents, 480000)
        self.assertEqual(goals.load(self.path, CATS).liquid_cents, 480000)

    def test_whole_euros_stay_whole_in_the_file(self):
        goals.set_limit("super", 35000, "2026-09", path=self.path, categories=CATS)
        self.assertIn("monthly_eur: 350\n", self.path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
