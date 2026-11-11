"""categories.yaml: load and validate. Every category reference in the codebase goes
through require(), so a typo in rules.yaml fails at load time and not halfway through
a categorization run."""

from dataclasses import dataclass
from pathlib import Path

import yaml

CATEGORIES_PATH = Path("categories.yaml")
TYPES = ("expense", "income", "internal")


# Two flags the metrics read, declared here rather than hardcoded in metrics.py so
# that reclassifying a category is a taxonomy edit and so that a typo fails at load
# time like every other category reference.
#   fixed          a commitment that recurs whether or not the detector has enough
#                  history to have noticed it yet (milestone 3.3)
#   discretionary  spending that could stop tomorrow without changing how you live
#   opaque         money whose purpose the ledger cannot see (cash withdrawn, Bizums
#                  with no concept line). The monthly review reports its share of
#                  spend, because judging the visible part of a month while a large
#                  slice went to cash is judging half of it
# Children inherit the parent's value and may override it: salud is not fixed, but
# salud-gimnasio is.
FLAGS = ("fixed", "discretionary", "opaque")


@dataclass(frozen=True)
class Category:
    id: str
    name: str
    type: str
    parent: str | None = None
    fixed: bool = False
    discretionary: bool = False
    opaque: bool = False


def load(path: Path = CATEGORIES_PATH) -> dict[str, Category]:
    entries = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise ValueError(f"{path}: expected a list of categories")

    # Two passes: parents must exist before children can inherit their type, and the
    # file is ordered parent-first only by convention.
    raw: dict[str, dict] = {}
    for e in entries:
        unknown = set(e) - ({"id", "name", "parent", "type"} | set(FLAGS))
        if unknown:
            raise ValueError(f"category {e.get('id')!r}: unknown keys {sorted(unknown)}")
        if not e.get("id") or not e.get("name"):
            raise ValueError(f"category {e!r}: id and name are required")
        if e["id"] in raw:
            raise ValueError(f"duplicate category id {e['id']!r}")
        raw[e["id"]] = e

    out: dict[str, Category] = {}
    for cid, e in raw.items():
        parent = e.get("parent")
        if parent is not None and parent not in raw:
            raise ValueError(f"category {cid!r}: parent {parent!r} does not exist")
        # Two levels only. A deeper tree is harder to report on than it is worth.
        if parent is not None and raw[parent].get("parent") is not None:
            raise ValueError(f"category {cid!r}: parent {parent!r} is itself a child")
        ctype = e.get("type") or (raw[parent].get("type") if parent else None)
        if ctype not in TYPES:
            raise ValueError(f"category {cid!r}: type must be one of {TYPES}, got {ctype!r}")
        if parent and e.get("type") and e["type"] != raw[parent].get("type"):
            raise ValueError(f"category {cid!r}: type {e['type']!r} contradicts parent {parent!r}")
        flags = {}
        for flag in FLAGS:
            value = e.get(flag, raw[parent].get(flag) if parent else None)
            if value not in (None, True, False):
                raise ValueError(f"category {cid!r}: {flag} must be true or false, got {value!r}")
            flags[flag] = bool(value)
        out[cid] = Category(id=cid, name=e["name"], type=ctype, parent=parent, **flags)
    return out


def require(categories: dict[str, Category], cid: str, where: str) -> str:
    if cid not in categories:
        raise ValueError(f"{where}: unknown category {cid!r}. Add it to categories.yaml "
                         "or fix the reference; categories are never invented at runtime.")
    return cid


def spend_types(categories: dict[str, Category], cid: str | None) -> bool:
    """Whether a transaction with this category counts towards spending. Used by the
    metrics in milestone 3; uncategorized (None) counts, or spend would shrink every
    time a rule is missing."""
    return cid is None or categories[cid].type == "expense"
