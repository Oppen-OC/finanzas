"""The categorization engine (D7). rules.yaml is an ordered list, first match wins,
and the app applies it. Claude proposes rule entries; it never labels a transaction
directly, which is what keeps the result reproducible and auditable."""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from finanzas import manual as manual_mod
from finanzas.categories import Category, load as load_categories, require
from finanzas.fmt import cents

RULES_PATH = Path("rules.yaml")
_FIELDS = {"id", "match", "category", "account", "sign", "amount_min", "amount_max", "note"}


@dataclass(frozen=True)
class Rule:
    id: str
    pattern: re.Pattern
    category: str
    account: str | None = None
    sign: str | None = None            # '+' income side, '-' expense side
    amount_min: int | None = None      # inclusive, cents, compared against abs(amount)
    amount_max: int | None = None

    def matches(self, row) -> bool:
        amount = abs(row["amount_cents"])
        return (
            (self.account is None or row["account"] == self.account)
            and (self.sign is None or (row["amount_cents"] >= 0) == (self.sign == "+"))
            and (self.amount_min is None or amount >= self.amount_min)
            and (self.amount_max is None or amount <= self.amount_max)
            and bool(self.pattern.search(row["description_norm"]))
        )


def load(path: Path = RULES_PATH, categories: dict[str, Category] | None = None) -> list[Rule]:
    categories = categories if categories is not None else load_categories()
    entries = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if not isinstance(entries, list):
        raise ValueError(f"{path}: expected an ordered list of rules")

    rules, seen = [], set()
    for e in entries:
        where = f"{path} rule {e.get('id')!r}"
        unknown = set(e) - _FIELDS
        if unknown:
            raise ValueError(f"{where}: unknown keys {sorted(unknown)}")
        if not e.get("id") or not e.get("match") or not e.get("category"):
            raise ValueError(f"{where}: id, match and category are required")
        if e["id"] in seen:
            raise ValueError(f"{where}: duplicate rule id")
        seen.add(e["id"])
        if e.get("sign") not in (None, "+", "-"):
            raise ValueError(f"{where}: sign must be '+' or '-'")
        try:
            pattern = re.compile(e["match"])
        except re.error as err:
            raise ValueError(f"{where}: invalid regex: {err}") from err
        rules.append(Rule(
            id=e["id"],
            pattern=pattern,
            # A category that does not exist fails here, at load time, not against
            # transaction 200 of 296 with half the ledger already rewritten.
            category=require(categories, e["category"], where),
            account=e.get("account"),
            sign=e.get("sign"),
            amount_min=cents(e["amount_min"], where) if e.get("amount_min") is not None else None,
            amount_max=cents(e["amount_max"], where) if e.get("amount_max") is not None else None,
        ))
    return rules


def apply_rules(conn, rules: list[Rule], dry_run: bool = False,
                manual: dict[str, str] | None = None) -> dict:
    """Recompute every category from scratch: manual.yaml first, then the rules in
    order. Every category in the ledger is derived, so the files are the only source
    of truth and a rules.yaml edit always takes effect (without the clear, a
    transaction assigned by a rule that was later narrowed would keep its category
    forever)."""
    manual = manual_mod.load() if manual is None else manual
    rows = conn.execute("SELECT * FROM transactions").fetchall()

    matched = {r.id: 0 for r in rules}
    assignments, used_keys = [], set()
    for row in rows:
        key = manual_mod.key_for(row)
        if key in manual:
            used_keys.add(key)
            assignments.append((manual[key], "manual", row["id"]))
            continue
        for rule in rules:
            if rule.matches(row):
                matched[rule.id] += 1
                assignments.append((rule.category, f"rule:{rule.id}", row["id"]))
                break

    if not dry_run:
        with conn:
            conn.execute("UPDATE transactions SET category = NULL, category_src = NULL")
            conn.executemany(
                "UPDATE transactions SET category = ?, category_src = ? WHERE id = ?",
                assignments)

    # An entry matching nothing means the transaction it pinned is gone: a different
    # export of the same movement, or a ledger rebuilt from different files.
    return {"total": len(rows), "manual": len(used_keys), "by_rule": matched,
            "stale_manual": sorted(set(manual) - used_keys),
            "assigned": sum(1 for a in assignments if a[1] != "manual"),
            "uncategorized": len(rows) - len(assignments)}


def diagnose(conn, rules: list[Rule]) -> list[dict]:
    """Per rule: how many transactions it wins, and how many it would match if it
    were first. A rule file nobody audits rots: a rule matching nothing (the merchant
    renamed itself) and a rule matching only what an earlier rule already took (a
    broad rule written above a narrow one) both look fine from the outside."""
    rows = conn.execute(
        "SELECT account, amount_cents, description_norm FROM transactions").fetchall()
    stats = {r.id: {"id": r.id, "category": r.category, "assigned": 0, "would_match": 0}
             for r in rules}
    for row in rows:
        winner = None
        for rule in rules:
            if rule.matches(row):
                stats[rule.id]["would_match"] += 1
                if winner is None:
                    winner = rule.id
        if winner:
            stats[winner]["assigned"] += 1
    return list(stats.values())
