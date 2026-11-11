"""The rules engine's two load-bearing properties: the same rules.yaml always yields
the same assignment, and a category set by hand is never overwritten by a rule."""

import tempfile
import unittest
from pathlib import Path

from finanzas import db, manual, rules


def rules_from(text: str) -> list:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write(text)
    return rules.load(Path(f.name))


class ApplyRules(unittest.TestCase):
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

    def add(self, description_norm, amount_cents=-1000, account="bbva"):
        self.n += 1
        self.conn.execute(
            "INSERT INTO transactions (account, date, date_booked, amount_cents, balance_cents, "
            "occurrence, description, description_norm, raw_row_id) VALUES (?,?,?,?,?,1,?,?,1)",
            (account, "2026-09-01", "2026-09-01", amount_cents, self.n * 100,
             description_norm, description_norm))
        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def categories_of(self):
        return {r["id"]: (r["category"], r["category_src"])
                for r in self.conn.execute("SELECT id, category, category_src FROM transactions")}

    def test_first_match_wins_and_is_idempotent(self):
        rs = rules_from(
            "- {id: mercadona, match: mercadona, category: supermercado}\n"
            "- {id: contactless, match: contactless, category: misc}\n")
        tid = self.add("transaccion contactless en mercadona plaza")
        first = rules.apply_rules(self.conn, rs)
        self.assertEqual(self.categories_of()[tid], ("supermercado", "rule:mercadona"))
        second = rules.apply_rules(self.conn, rs)
        self.assertEqual(first, second)

    def test_narrowing_conditions(self):
        rs = rules_from("- {id: big_in, match: bizum, category: ingresos-otros, "
                        "sign: '+', amount_min: 50, account: bbva}\n")
        small = self.add("bizum / recibido: cine", amount_cents=500)
        big = self.add("bizum / recibido: premio", amount_cents=7500)
        out = self.add("bizum / enviado: paella", amount_cents=-7500)
        other = self.add("bizum / recibido: premio", amount_cents=7500, account="santander")
        rules.apply_rules(self.conn, rs)
        cats = self.categories_of()
        self.assertEqual(cats[big][0], "ingresos-otros")
        self.assertIsNone(cats[small][0])
        self.assertIsNone(cats[out][0])
        self.assertIsNone(cats[other][0])

    def test_manual_wins_over_rules_and_rule_categories_are_rewritten(self):
        tid = self.add("transaccion contactless en mercadona plaza")
        pinned = self.add("bizum / enviado: cosa rara")
        row = self.conn.execute("SELECT * FROM transactions WHERE id=?", (pinned,)).fetchone()
        entries = {manual.key_for(row): "compras-regalos"}
        rules.apply_rules(self.conn, rules_from(
            "- {id: mercadona, match: mercadona, category: supermercado}\n"
            "- {id: catchall, match: '.', category: misc}\n"), manual=entries)
        self.assertEqual(self.categories_of()[pinned], ("compras-regalos", "manual"))
        # A rule edit must take effect on a transaction a previous run already assigned.
        rules.apply_rules(self.conn, rules_from(
            "- {id: mercadona, match: mercadona, category: compras-general}\n"), manual=entries)
        self.assertEqual(self.categories_of()[tid], ("compras-general", "rule:mercadona"))
        self.assertEqual(self.categories_of()[pinned], ("compras-regalos", "manual"))

    def test_manual_entry_pointing_nowhere_is_reported(self):
        self.add("transaccion contactless en mercadona plaza")
        res = rules.apply_rules(self.conn, rules_from(
            "- {id: mercadona, match: mercadona, category: supermercado}\n"),
            manual={"deadbeef1234": "efectivo"})
        self.assertEqual(res["stale_manual"], ["deadbeef1234"])

    def test_manual_key_ignores_the_row_id(self):
        """Ids are handed out by import order and change on a rebuild, so the key
        must depend only on the natural key: account, date, amounts, text."""
        first = self.add("compra en algun sitio", amount_cents=-1234)
        row = self.conn.execute("SELECT * FROM transactions WHERE id=?", (first,)).fetchone()
        self.conn.execute(
            "INSERT INTO transactions (id, account, date, date_booked, amount_cents, "
            "balance_cents, occurrence, description, description_norm, raw_row_id) "
            "SELECT 999, account, date, date_booked, amount_cents, balance_cents, "
            "occurrence + 1, description, description_norm, raw_row_id "
            "FROM transactions WHERE id = ?", (first,))
        same = self.conn.execute("SELECT * FROM transactions WHERE id=999").fetchone()
        self.assertEqual(manual.key_for(row), manual.key_for(dict(row) | {"id": 999}))
        # occurrence is part of the identity, so the repeat is a different key
        self.assertNotEqual(manual.key_for(row), manual.key_for(same))

    def test_unknown_category_raises_at_load_time(self):
        with self.assertRaises(ValueError):
            rules_from("- {id: x, match: mercadona, category: supermecado}\n")

    def test_invalid_regex_raises_at_load_time(self):
        with self.assertRaises(ValueError):
            rules_from("- {id: x, match: '[unclosed', category: supermercado}\n")


if __name__ == "__main__":
    unittest.main()
