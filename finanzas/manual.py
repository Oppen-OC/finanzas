"""Hand-set categories, kept in manual.yaml rather than in the database.

The database is disposable: it can always be rebuilt by deleting it and re-importing
the inbox. A category set by hand and stored only in the ledger would not survive
that, and the whole point of the design is that the ledger is reproducible from
files. So the files are the source of truth: inbox + rules.yaml + manual.yaml fully
determine every category, and `categorize` derives the rest.

Entries are keyed by the transaction's natural key (the dedupe key), not by row id,
because row ids are assigned by import order and change on a rebuild."""

import hashlib
from pathlib import Path

import yaml

from finanzas.categories import Category, load as load_categories, require

MANUAL_PATH = Path("manual.yaml")


def key_for(row) -> str:
    """Stable identity of a transaction across rebuilds: the same tuple the dedupe
    uses. Short hash, because the tuple itself is unreadable in a YAML key and the
    entry carries a human-readable note anyway."""
    raw = "|".join(str(row[f]) for f in ("account", "date", "amount_cents",
                                         "balance_cents", "description_norm", "occurrence"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def load(path: Path = MANUAL_PATH, categories: dict[str, Category] | None = None) -> dict[str, str]:
    if not path.exists():
        return {}
    categories = categories if categories is not None else load_categories()
    entries = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    out = {}
    for e in entries:
        where = f"{path} entry {e.get('key')!r}"
        if not e.get("key") or not e.get("category"):
            raise ValueError(f"{where}: key and category are required")
        if e["key"] in out:
            raise ValueError(f"{where}: duplicate key")
        out[e["key"]] = require(categories, e["category"], where)
    return out


def set_category(conn, txn_id: int, category: str, note: str | None = None,
                 path: Path = MANUAL_PATH) -> dict:
    """Add or replace one entry. Written by hand through `categorize --set`, because
    computing the key by hand is not something anyone should have to do."""
    row = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    if row is None:
        raise ValueError(f"no transaction with id {txn_id}")
    require(load_categories(), category, f"--set {txn_id}")

    key = key_for(row)
    entries = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else []
    entries = [e for e in (entries or []) if e.get("key") != key]
    entries.append({
        "key": key,
        "category": category,
        # Not read by anything: it is what makes the file reviewable in a diff.
        "note": note or f"{row['date']} {row['amount_cents'] / 100:.2f} EUR "
                        f"{row['account']} {row['description'][:60]}",
    })
    path.write_text(
        "# Categories set by hand, keyed by the transaction's natural key so they\n"
        "# survive deleting and rebuilding the database. `categorize` applies these\n"
        "# first; rules never overwrite them. Edit through `categorize --set`.\n"
        + yaml.safe_dump(entries, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    return {"key": key, "category": category}
