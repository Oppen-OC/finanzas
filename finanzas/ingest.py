"""Additive, idempotent import (constraint 5): new rows are inserted, known rows are
counted as duplicates. Nothing is ever updated or deleted."""

import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

import yaml

from finanzas import parsers
from finanzas.clean import normalize
from finanzas.parsers import ParsedFile

ACCOUNTS_PATH = Path("accounts.yaml")


def resolve_account(parsed: ParsedFile, accounts: dict) -> str:
    """Map a file to an account via accounts.yaml. Refuses to guess: an unknown card or
    IBAN stops the import, because silently opening a new account would split one
    balance chain in two and hide gaps."""
    candidates = {name: set(a["ids"]) for name, a in accounts.items() if a["bank"] == parsed.bank}
    if not candidates:
        raise ValueError(f"no account for bank {parsed.bank!r} in accounts.yaml")
    if not parsed.identifiers:  # e.g. a BBVA file with only Bizum rows
        if len(candidates) == 1:
            return next(iter(candidates))
        raise ValueError(f"file reveals no identifiers and {parsed.bank} has several accounts")
    matched = {name for name, ids in candidates.items() if ids & parsed.identifiers}
    unknown = parsed.identifiers - set().union(*candidates.values())
    if unknown:
        raise ValueError(f"unknown {parsed.bank} identifiers {sorted(unknown)}: "
                         "add them to the right account in accounts.yaml")
    if len(matched) != 1:
        raise ValueError(f"identifiers {sorted(parsed.identifiers)} match accounts {sorted(matched)}")
    return matched.pop()


def check_file_chain(parsed: ParsedFile) -> None:
    """Every row's balance must equal the previous balance plus its amount. If this
    fails the parser misread something, and nothing from the file goes in."""
    for prev, row in zip(parsed.rows, parsed.rows[1:]):
        if prev.balance_cents + row.amount_cents != row.balance_cents:
            raise ValueError(f"balance chain broken inside file at {row.date_booked} "
                             f"{row.description!r}: {prev.balance_cents} + {row.amount_cents} "
                             f"!= {row.balance_cents}")


def import_file(conn, path: Path, accounts: dict) -> dict:
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    if conn.execute("SELECT 1 FROM imports WHERE sha256 = ?", (sha,)).fetchone():
        return {"file": path.name, "status": "skipped (already imported)"}

    parsed = parsers.parse(path)
    if not parsed.rows:
        raise ValueError(f"{path.name}: no transactions found")
    check_file_chain(parsed)
    account = resolve_account(parsed, accounts)

    counts = Counter()
    occurrences = Counter()
    with conn:  # one transaction per file: a failure leaves no partial import behind
        import_id = conn.execute(
            "INSERT INTO imports (sha256, filename, bank, account, imported_at, date_min, "
            "date_max, n_rows, n_inserted, n_duplicate) VALUES (?,?,?,?,?,?,?,?,0,0)",
            (sha, path.name, parsed.bank, account, datetime.now().isoformat(timespec="seconds"),
             min(r.date for r in parsed.rows), max(r.date for r in parsed.rows), len(parsed.rows)),
        ).lastrowid

        for pos, row in enumerate(parsed.rows):
            raw_id = conn.execute(
                "INSERT INTO raw_rows (import_id, position, data) VALUES (?,?,?)",
                (import_id, pos, json.dumps(row.raw, ensure_ascii=False)),
            ).lastrowid
            norm = normalize(row.description)
            key = (account, row.date, row.amount_cents, row.balance_cents, norm)
            occurrences[key] += 1
            existing = conn.execute(
                "SELECT 1 FROM transactions WHERE account=? AND date=? AND amount_cents=? "
                "AND balance_cents=? AND description_norm=? AND occurrence=?",
                (*key, occurrences[key]),
            ).fetchone()

            # A reworded re-export of a row already stored lands here as a new row and
            # is double counted; check.walk_chain is what surfaces that (D11).
            if existing is None:
                conn.execute(
                    "INSERT INTO transactions (account, date, date_booked, amount_cents, "
                    "balance_cents, occurrence, description, description_norm, raw_row_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (account, row.date, row.date_booked, row.amount_cents, row.balance_cents,
                     occurrences[key], row.description, norm, raw_id),
                )
                counts["inserted"] += 1
            else:
                counts["duplicate"] += 1

        conn.execute(
            "UPDATE imports SET n_inserted=?, n_duplicate=? WHERE id=?",
            (counts["inserted"], counts["duplicate"], import_id),
        )
    return {"file": path.name, "status": "imported", "account": account,
            "rows": len(parsed.rows), **counts}


def load_accounts(path: Path = ACCOUNTS_PATH) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))
