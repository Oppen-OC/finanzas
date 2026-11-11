"""goals.yaml: the limits you set yourself, and the settings the metrics read.

A file, not the `goals` table first planned, for the reason D15 already
settled about hand-set categories: the database is disposable and gets rebuilt
from the inbox, and deleting it is a verification step that actually gets run.
A limit stored only there dies with the next rebuild. Limits are configuration
rather than derived state, so they belong next to categories.yaml, rules.yaml
and manual.yaml, and the sentence that describes the whole design stays true:
the files determine everything the app does.

A limit carries the month it starts from. Raising the groceries limit in
November must not retroactively make October look disciplined, so the limit in
force for a month is the latest entry starting at or before it. `monthly_eur:
null` is a real entry, meaning the limit was lifted from that month on; it only
exists when there is earlier history worth keeping, since a category that never
had a limit needs no record saying so.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from finanzas.categories import Category, load as load_categories, require
from finanzas.fmt import cents

GOALS_PATH = Path("goals.yaml")


def _path(path: Path | None) -> Path:
    # Resolved at call time, never frozen into a default argument: db.py was bitten
    # by exactly that, and the web UI's tests have to be able to point the module
    # at a fixture without writing over the real goals.yaml.
    return Path(path) if path is not None else GOALS_PATH
MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_LIMIT_FIELDS = {"category", "monthly_eur", "since", "note"}
_SETTINGS = {"liquid_eur"}

HEADER = """\
# Limits you set for yourself, and the settings the metrics read. Edited through
# `finanzas goals` or the Objetivos page; kept in a file rather than in the
# database because the database is rebuilt from the inbox and a limit stored
# there would not survive it (see D15, the same argument for manual.yaml).
#
# `since` is the month a limit starts applying. The limit in force for a month
# is the latest entry starting at or before it, so changing a limit today does
# not rewrite the verdict on last month. A null monthly_eur lifts the limit from
# that month on.
"""


@dataclass(frozen=True)
class Limit:
    category: str
    monthly_cents: int | None   # None means the limit was lifted from `since`
    since: str                  # YYYY-MM
    note: str | None = None


@dataclass(frozen=True)
class Goals:
    limits: tuple[Limit, ...] = ()
    # Overrides the sum of account balances in the runway metric, for when part
    # of that money is not really available cash (O2). None means use the sum.
    liquid_cents: int | None = None

    def at(self, month: str) -> dict[str, Limit]:
        """The limits in force in `month`, by category. Later entries win, and an
        entry that lifts the limit leaves the category out entirely."""
        out: dict[str, Limit] = {}
        for lim in sorted(self.limits, key=lambda l: l.since):
            if lim.since <= month:
                out[lim.category] = lim
        return {c: l for c, l in out.items() if l.monthly_cents is not None}

    def history(self, category: str) -> list[Limit]:
        return sorted((l for l in self.limits if l.category == category),
                      key=lambda l: l.since)


def load(path: Path | None = None, categories: dict[str, Category] | None = None) -> Goals:
    """Absent file means no limits and no override, which is the state the tool
    ships in: goals.yaml is written the first time something is set."""
    path = _path(path)
    if not path.exists():
        return Goals()
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: expected a mapping with 'limits' and 'settings'")
    unknown = set(doc) - {"limits", "settings"}
    if unknown:
        raise ValueError(f"{path}: unknown keys {sorted(unknown)}")

    categories = categories if categories is not None else load_categories()

    settings = doc.get("settings") or {}
    unknown = set(settings) - _SETTINGS
    if unknown:
        raise ValueError(f"{path} settings: unknown keys {sorted(unknown)}")
    liquid = settings.get("liquid_eur")
    liquid = None if liquid is None else cents(liquid, f"{path} settings.liquid_eur")

    limits, seen = [], set()
    for e in doc.get("limits") or []:
        where = f"{path} limit {e.get('category')!r}"
        if not isinstance(e, dict):
            raise ValueError(f"{path}: every limit must be a mapping, got {e!r}")
        unknown = set(e) - _LIMIT_FIELDS
        if unknown:
            raise ValueError(f"{where}: unknown keys {sorted(unknown)}")
        if not e.get("category") or not e.get("since"):
            raise ValueError(f"{where}: category and since are required")
        # Validated here rather than where the limit is used: a typo in a
        # category id must fail on load like every other category reference,
        # not halfway through rendering a dashboard.
        require(categories, e["category"], where)
        since = str(e["since"])
        if not MONTH.match(since):
            raise ValueError(f"{where}: since must be YYYY-MM, got {since!r}")
        if (e["category"], since) in seen:
            raise ValueError(f"{where}: two limits starting in {since}")
        seen.add((e["category"], since))
        amount = e.get("monthly_eur")
        value = None if amount is None else cents(amount, where)
        if value is not None and value <= 0:
            raise ValueError(f"{where}: a limit of {amount!r} is not a limit. "
                             "Write monthly_eur: null to lift it.")
        limits.append(Limit(e["category"], value, since, e.get("note")))

    return Goals(tuple(sorted(limits, key=lambda l: (l.category, l.since))), liquid)


def _eur(c: int | None):
    """Back to euros for the file, as an int when it is whole: goals.yaml is read
    and edited by hand, and `350` reads better than `350.0`."""
    if c is None:
        return None
    return c // 100 if c % 100 == 0 else c / 100


def _dump(goals: Goals, path: Path | None = None) -> Goals:
    path = _path(path)
    doc = {
        "settings": {"liquid_eur": _eur(goals.liquid_cents)},
        "limits": [
            {k: v for k, v in (("category", l.category),
                               ("monthly_eur", _eur(l.monthly_cents)),
                               ("since", l.since),
                               ("note", l.note)) if not (k == "note" and v is None)}
            for l in goals.limits],
    }
    path.write_text(HEADER + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    return goals


def set_limit(category: str, monthly_cents: int | None, since: str,
              note: str | None = None, path: Path | None = None,
              categories: dict[str, Category] | None = None) -> Goals:
    """Set (or lift, with None) a limit from `since` on.

    An entry already starting in that month is replaced rather than stacked:
    changing your mind twice in September is one decision, not two."""
    categories = categories if categories is not None else load_categories()
    require(categories, category, f"goals --set {category}")
    if not MONTH.match(since):
        raise ValueError(f"since must be YYYY-MM, got {since!r}")
    if monthly_cents is not None and monthly_cents <= 0:
        raise ValueError("a limit of zero or less is not a limit; unset it instead")

    current = load(path, categories)
    limits = [l for l in current.limits
              if not (l.category == category and l.since == since)]
    limits.append(Limit(category, monthly_cents, since, note))
    # A category whose every entry lifts the limit is just a category with no
    # limit, and the file should not carry a record of a default.
    if all(l.monthly_cents is None for l in limits if l.category == category):
        limits = [l for l in limits if l.category != category]
    return _dump(Goals(tuple(sorted(limits, key=lambda l: (l.category, l.since))),
                       current.liquid_cents), path)


def unset_limit(category: str, since: str, path: Path | None = None,
                categories: dict[str, Category] | None = None) -> Goals:
    return set_limit(category, None, since, None, path, categories)


def set_liquid(liquid_cents: int | None, path: Path | None = None,
               categories: dict[str, Category] | None = None) -> Goals:
    current = load(path, categories)
    return _dump(Goals(current.limits, liquid_cents), path)
