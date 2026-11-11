"""A backup nobody has restored is not a backup. The property here is that the
copy opens as a database and holds the same ledger, not merely that a file
appeared."""

import argparse
import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from finanzas import cli, db


class Backup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        self.conn.execute("INSERT INTO imports (id, sha256, filename, bank, account, imported_at, "
                          "n_rows, n_inserted, n_duplicate) VALUES (1,'x','x.xls','bbva','bbva',"
                          "'2026-09-01T00:00:00',0,0,0)")
        self.conn.execute("INSERT INTO raw_rows (id, import_id, position, data) VALUES (1,1,0,'{}')")
        self.conn.execute(
            "INSERT INTO transactions (account, date, date_booked, amount_cents, balance_cents, "
            "occurrence, description, description_norm, raw_row_id, category, category_src) "
            "VALUES ('bbva','2026-08-01','2026-08-01',-3000,1000,1,'mercadona','mercadona',1,"
            "'supermercado','rule:t')")

    def backup_to(self, out: Path) -> None:
        # The command is written to be read in a terminal, and the suite is not
        # one: its two lines of output would bury the test names.
        with contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_backup(self.conn, argparse.Namespace(dir=str(out)))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_the_copy_is_the_same_ledger(self):
        out = Path(self.tmp.name) / "backups"
        self.backup_to(out)
        copies = list(out.glob("finanzas-*.db"))
        self.assertEqual(len(copies), 1)
        restored = sqlite3.connect(copies[0])
        self.assertEqual(list(restored.iterdump()), list(self.conn.iterdump()))
        restored.close()

    def test_a_pending_write_does_not_hang_the_backup(self):
        # sqlite3's backup retries a busy source forever and takes no timeout, so an
        # open transaction on the source is not a slow backup, it is a command that
        # never returns. cmd_backup closes the transaction before it starts.
        self.conn.execute(
            "INSERT INTO transactions (account, date, date_booked, amount_cents, balance_cents, "
            "occurrence, description, description_norm, raw_row_id) "
            "VALUES ('bbva','2026-08-02','2026-08-02',-500,500,1,'bar','bar',1)")
        out = Path(self.tmp.name) / "backups"
        self.backup_to(out)
        copy = next(out.glob("finanzas-*.db"))
        with contextlib.closing(sqlite3.connect(copy)) as restored:
            n = restored.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        self.assertEqual(n, 2)


if __name__ == "__main__":
    unittest.main()
