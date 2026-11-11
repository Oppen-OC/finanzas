"""scenarios.yaml: what-if cash flow scenarios for the projection (D31).

Configuration, not derived state, so it is a file beside goals.yaml and not a
table, for the reason D25 gave: the database is rebuilt from the inbox and a
scenario stored there would not survive it. Edited by hand; the Escenarios page
only reads it.

Every event is signed euros (negative is money leaving) and is either
recurring, `monthly_eur` with an optional inclusive `from` and `until`, or a
one-off, `eur` in a single `month`. A recurring event may name a category it
`replaces`: while it is active that category is taken out of the baseline
outflow, so rent added on top of a burn that already holds some housing is not
counted twice.

Everything is checked here, at load, so a typo fails with the scenario and the
event named rather than as a wrong line on a chart.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from finanzas.categories import Category, load as load_categories, require
from finanzas.fmt import cents
from finanzas.goals import MONTH

SCENARIOS_PATH = Path("scenarios.yaml")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_TOP = {"horizon_months", "min_balance_eur", "scenarios"}
_SCENARIO = {"id", "name", "events"}
_EVENT = {"label", "monthly_eur", "eur", "from", "until", "month", "replaces"}
# Long enough for any job search, short enough that a typo of 180 for 18 is caught.
MAX_HORIZON = 60


def _path(path: Path | None) -> Path:
    # Resolved at call time so the web tests can point it at a fixture (see goals.py).
    return Path(path) if path is not None else SCENARIOS_PATH


@dataclass(frozen=True)
class Event:
    label: str
    cents: int                 # signed: negative is an outflow
    recurring: bool
    start: str | None = None   # YYYY-MM, inclusive; None means from the first projected month
    end: str | None = None     # YYYY-MM, inclusive; None means open-ended
    replaces: str | None = None

    def active(self, month: str) -> bool:
        return ((self.start is None or self.start <= month)
                and (self.end is None or month <= self.end))

    @property
    def steady(self) -> bool:
        """Still running once every start date has passed: recurring and with no
        end. The break-even is computed over these alone, because an event with
        an `until` (a fixed-term contract) is by definition not part of a steady state."""
        return self.recurring and self.end is None


@dataclass(frozen=True)
class Scenario:
    id: str
    name: str
    events: tuple[Event, ...]


@dataclass(frozen=True)
class Config:
    horizon_months: int
    floor_cents: int
    scenarios: tuple[Scenario, ...]


def _month(value, where: str, key: str) -> str:
    # YAML reads 2026-11 as a string but 2026-11-01 as a date; str() then the
    # regex refuses the latter with a message instead of a silent mismatch.
    m = str(value)
    if not MONTH.match(m):
        raise ValueError(f"{where}: {key} must be YYYY-MM, got {value!r}")
    return m


def _event(e, where: str, categories: dict[str, Category]) -> Event:
    if not isinstance(e, dict):
        raise ValueError(f"{where}: every event must be a mapping, got {e!r}")
    where = f"{where} event {e.get('label')!r}"
    unknown = set(e) - _EVENT
    if unknown:
        raise ValueError(f"{where}: unknown keys {sorted(unknown)}")
    if not e.get("label"):
        raise ValueError(f"{where}: label is required")
    has_monthly, has_once = e.get("monthly_eur") is not None, e.get("eur") is not None
    if has_monthly == has_once:
        raise ValueError(f"{where}: set exactly one of monthly_eur (recurring) and eur (one-off)")

    if has_once:
        if "from" in e or "until" in e or "replaces" in e:
            raise ValueError(f"{where}: a one-off event takes `month`, not from/until/replaces")
        if "month" not in e:
            raise ValueError(f"{where}: a one-off event needs `month`")
        m = _month(e["month"], where, "month")
        return Event(str(e["label"]), cents(e["eur"], where), False, m, m)

    if "month" in e:
        raise ValueError(f"{where}: a recurring event takes from/until, not `month`")
    start = _month(e["from"], where, "from") if e.get("from") is not None else None
    end = _month(e["until"], where, "until") if e.get("until") is not None else None
    if start and end and end < start:
        raise ValueError(f"{where}: until {end} is before from {start}")
    replaces = e.get("replaces")
    if replaces is not None:
        require(categories, replaces, where)
        # Taking an income or an internal category out of an outflow baseline is
        # meaningless; refusing it is cheaper than explaining the resulting line.
        if categories[replaces].type != "expense":
            raise ValueError(f"{where}: replaces {replaces!r}, which is not an expense category")
    return Event(str(e["label"]), cents(e["monthly_eur"], where), True, start, end, replaces)


def load(path: Path | None = None, categories: dict[str, Category] | None = None) -> Config | None:
    """None when the file does not exist: the page then says how to create one.
    Anything present but wrong raises ValueError naming the scenario and event."""
    path = _path(path)
    if not path.exists():
        return None
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: expected a mapping with horizon_months, min_balance_eur "
                         "and scenarios")
    unknown = set(doc) - _TOP
    if unknown:
        raise ValueError(f"{path}: unknown keys {sorted(unknown)}")
    categories = categories if categories is not None else load_categories()

    horizon = doc.get("horizon_months", 18)
    if not isinstance(horizon, int) or isinstance(horizon, bool) or not 1 <= horizon <= MAX_HORIZON:
        raise ValueError(f"{path}: horizon_months must be a whole number from 1 to "
                         f"{MAX_HORIZON}, got {horizon!r}")
    floor = cents(doc.get("min_balance_eur", 0), f"{path} min_balance_eur")
    if floor < 0:
        raise ValueError(f"{path}: min_balance_eur cannot be negative")

    scenarios, seen = [], set()
    for s in doc.get("scenarios") or []:
        if not isinstance(s, dict):
            raise ValueError(f"{path}: every scenario must be a mapping, got {s!r}")
        sid = str(s.get("id") or "")
        where = f"{path} scenario {sid!r}"
        unknown = set(s) - _SCENARIO
        if unknown:
            raise ValueError(f"{where}: unknown keys {sorted(unknown)}")
        # A slug because it goes into a URL (?s=) and a CLI flag.
        if not SLUG.match(sid):
            raise ValueError(f"{where}: id must be lowercase letters, digits and hyphens")
        if sid in seen:
            raise ValueError(f"{where}: duplicate scenario id")
        seen.add(sid)
        events = tuple(_event(e, where, categories) for e in (s.get("events") or []))
        scenarios.append(Scenario(sid, str(s.get("name") or sid), events))
    if not scenarios:
        raise ValueError(f"{path}: no scenarios defined")
    return Config(horizon, floor, tuple(scenarios))
