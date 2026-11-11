"""SQLite schema. Three layers (D1): imports -> raw_rows -> transactions."""

import os
import sqlite3
from pathlib import Path

# Overridable so tests and experiments never touch the real ledger.
DB_PATH = Path(os.environ.get("FINANZAS_DB", "data/finanzas.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
    id            INTEGER PRIMARY KEY,
    sha256        TEXT NOT NULL UNIQUE,     -- same file twice is a no-op
    filename      TEXT NOT NULL,
    bank          TEXT NOT NULL,
    account       TEXT NOT NULL,
    imported_at   TEXT NOT NULL,
    date_min      TEXT,
    date_max      TEXT,
    n_rows        INTEGER NOT NULL,
    n_inserted    INTEGER NOT NULL,
    n_duplicate   INTEGER NOT NULL
);

-- Every row of every file exactly as the bank sent it (masked), so a parsing or
-- dedupe mistake can be replayed later without the original export.
CREATE TABLE IF NOT EXISTS raw_rows (
    id         INTEGER PRIMARY KEY,
    import_id  INTEGER NOT NULL REFERENCES imports(id),
    position   INTEGER NOT NULL,            -- 0 = oldest row in the file
    data       TEXT NOT NULL                -- JSON
);

CREATE TABLE IF NOT EXISTS transactions (
    id               INTEGER PRIMARY KEY,
    account          TEXT NOT NULL,
    date             TEXT NOT NULL,         -- value date (D5)
    date_booked      TEXT NOT NULL,
    amount_cents     INTEGER NOT NULL,
    balance_cents    INTEGER NOT NULL,
    occurrence       INTEGER NOT NULL,
    description      TEXT NOT NULL,
    description_norm TEXT NOT NULL,
    raw_row_id       INTEGER NOT NULL REFERENCES raw_rows(id),
    -- Derived state, not bank data: rewritten by `categorize`, never by import.
    -- category_src is 'rule:<rule_id>' or 'manual'.
    category         TEXT,
    category_src     TEXT,
    -- D2: the balance tells apart identical-looking repeats (four equal Bizums on one
    -- day). Different text means a different transaction. occurrence covers the last
    -- corner: a balance returning to the same value on the same day with the same
    -- amount and text (+5, -5, +5).
    UNIQUE (account, date, amount_cents, balance_cents, description_norm, occurrence)
);
"""

# Columns added after milestone 1. CREATE TABLE IF NOT EXISTS leaves an existing table
# alone, so a ledger built before them needs the ALTER; without this every existing
# DB would have to be rebuilt from the inbox.
_ADDED_COLUMNS = {"category": "TEXT", "category_src": "TEXT"}


def connect(path: Path | None = None) -> sqlite3.Connection:
    # Resolved at call time, not bound as a default: a default argument would
    # freeze DB_PATH at import and leave no way to point a running process at
    # another ledger, which is exactly what the web UI's tests need.
    path = Path(path) if path is not None else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    have = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}
    for col, typ in _ADDED_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {col} {typ}")
    return conn
