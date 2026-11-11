"""Scenario tests against the BBVA export in data/inbox (skipped if absent): your
own, or the synthetic one demo/make_exports.py writes.

Overlapping and gapped exports are simulated by slicing a parsed file, so the
data is real but the scenarios are controlled. Run: uv run python -m unittest -v
"""

import dataclasses
import sqlite3
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

import pandas as pd

from finanzas import db, parsers
from finanzas.check import unmasked_digit_runs, walk_chain
from finanzas.clean import mask
from finanzas.ingest import import_file, load_accounts, resolve_account

INBOX = Path("data/inbox")


def _bbva_export():
    # By sheet name, not extension: the demo writes both banks as .xlsx, and glob
    # order is arbitrary across filesystems.
    for p in sorted(INBOX.glob("*.xlsx")):
        with pd.ExcelFile(p) as xf:
            if parsers.bbva.detect(xf.sheet_names):
                return p
    return None


BBVA = _bbva_export()


@unittest.skipIf(BBVA is None, "no BBVA export in data/inbox")
class IngestScenarios(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.full = parsers.parse(BBVA)
        cls.accounts = load_accounts()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        self.files = {}

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def fake_file(self, name, rows):
        """A file whose parse result is `rows`; distinct content so the hash differs."""
        path = Path(self.tmp.name) / name
        path.write_text(name)
        self.files[path] = dataclasses.replace(self.full, rows=rows)
        return path

    def ingest(self, path):
        with mock.patch.object(parsers, "parse", lambda p: self.files[p]):
            return import_file(self.conn, path, self.accounts)

    def count(self):
        return self.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

    def test_overlapping_exports_merge_without_loss(self):
        rows = self.full.rows
        r1 = self.ingest(self.fake_file("old", rows[:30]))
        r2 = self.ingest(self.fake_file("new", rows[15:]))
        self.assertEqual((r1["inserted"], r2["inserted"], r2["duplicate"]),
                         (30, len(rows) - 30, 15))
        self.assertEqual(self.count(), len(rows))
        self.assertEqual(walk_chain(self.conn, "bbva")[1], [])

    def test_identical_rows_are_kept_apart(self):
        # D2: identical rows on one day (four equal Bizums) are separate movements.
        groups = Counter((r.date, r.amount_cents, r.description) for r in self.full.rows)
        (date, amount, desc), size = groups.most_common(1)[0]
        if size < 2:
            self.skipTest("export has no identical rows on one day")
        self.ingest(self.fake_file("all", self.full.rows))
        n = self.conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE date=? AND amount_cents=? AND description=?",
            (date, amount, desc)).fetchone()[0]
        self.assertEqual(n, size)

    def test_gap_is_reported_with_missing_amount(self):
        rows = self.full.rows
        self.ingest(self.fake_file("a", rows[:10]))
        self.ingest(self.fake_file("b", rows[16:]))
        breaks = walk_chain(self.conn, "bbva")[1]
        self.assertEqual(len(breaks), 1)
        self.assertEqual(breaks[0][2], sum(r.amount_cents for r in rows[10:16]))

    def test_rewritten_balance_is_caught_by_chain(self):
        # The risk behind D2: if the bank shifted past balances between exports, the
        # overlap would be inserted twice. The key cannot see it; the chain must.
        rows = self.full.rows
        self.ingest(self.fake_file("old", rows[:30]))
        shifted = [dataclasses.replace(r, balance_cents=r.balance_cents + 500) for r in rows[15:]]
        self.ingest(self.fake_file("new", shifted))
        self.assertGreater(self.count(), len(rows))  # the double count happened...
        self.assertTrue(walk_chain(self.conn, "bbva")[1])  # ...and check flags it

    def test_different_text_is_a_different_transaction(self):
        # Same date, amount and balance but other wording: stored as new. If it was in
        # fact the bank rewording a known row, the extra amount breaks the chain.
        rows = self.full.rows
        self.ingest(self.fake_file("old", rows))
        reworded = rows[:5] + [dataclasses.replace(rows[5], description="something else")]
        r = self.ingest(self.fake_file("new", reworded))
        self.assertEqual((r.get("inserted", 0), r["duplicate"]), (1, 5))
        self.assertTrue(walk_chain(self.conn, "bbva")[1])

    def test_bad_file_chain_is_refused_whole(self):
        rows = list(self.full.rows)
        rows[3] = dataclasses.replace(rows[3], amount_cents=rows[3].amount_cents + 1)
        with self.assertRaises(ValueError):
            self.ingest(self.fake_file("bad", rows))
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0], 0)

    def test_no_long_digits_persisted(self):
        self.ingest(self.fake_file("all", self.full.rows))
        self.assertEqual(unmasked_digit_runs(self.conn), [])


class Accounts(unittest.TestCase):
    ACCOUNTS = {"bbva": {"bank": "bbva", "ids": ["0002"]}}

    def parsed(self, ids):
        return parsers.ParsedFile("bbva", set(ids))

    def test_known_card(self):
        self.assertEqual(resolve_account(self.parsed({"0002"}), self.ACCOUNTS), "bbva")

    def test_replaced_card_stops_import(self):
        with self.assertRaisesRegex(ValueError, "unknown"):
            resolve_account(self.parsed({"0002", "9999"}), self.ACCOUNTS)

    def test_bizum_only_file_falls_back_to_single_account(self):
        self.assertEqual(resolve_account(self.parsed(set()), self.ACCOUNTS), "bbva")


class Masking(unittest.TestCase):
    def test_patterns(self):
        self.assertEqual(mask("Tarjeta 4111111111111111 ,"), "Tarjeta *1111 ,")
        self.assertEqual(mask("ES0000000000000000000001"), "ES*0001")  # 22-digit body
        self.assertEqual(mask("pan 4111 1111 1111 1111"), "pan *1111")
        self.assertEqual(mask("Tarj. :*111111"), "Tarj. :*1111")
        self.assertEqual(mask("Bizum 675.24 on 10/09/2026"), "Bizum 675.24 on 10/09/2026")


if __name__ == "__main__":
    unittest.main()
