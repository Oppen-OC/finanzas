"""Bank parsers. Adding a bank = one module here exposing BANK, detect() and parse(),
plus one line in PARSERS."""

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd


@dataclass
class Row:
    raw: dict              # every cell of the bank row, already masked
    date: str              # value date, ISO (D5)
    date_booked: str       # booking date, ISO
    amount_cents: int
    balance_cents: int     # running balance after this movement (D2)
    description: str


@dataclass
class ParsedFile:
    bank: str
    identifiers: set[str]  # last-4 digits the file reveals (IBAN, cards), for accounts.yaml
    rows: list[Row] = field(default_factory=list)  # oldest first


def table_below(sheet: pd.DataFrame, first_header: str) -> tuple[list[str], pd.DataFrame]:
    """Find the header row by its first label instead of a fixed offset, so a change
    in preamble length does not silently shift every column."""
    for i, row in sheet.iterrows():
        cells = [str(c).strip() for c in row.tolist()]
        if first_header in cells:
            start = cells.index(first_header)
            headers, seen = [], {}
            for h in cells[start:]:
                seen[h] = seen.get(h, 0) + 1
                headers.append(h if seen[h] == 1 else f"{h}_{seen[h]}")  # BBVA repeats "Divisa"
            body = sheet.iloc[i + 1:, start:start + len(headers)]
            body.columns = headers
            return headers, body.dropna(how="all")
    raise ValueError(f"header {first_header!r} not found")


from finanzas.parsers import bbva, santander  # noqa: E402  (need Row defined first)

PARSERS = [santander, bbva]


def parse(path: Path) -> ParsedFile:
    with pd.ExcelFile(path) as xf:
        sheets = xf.sheet_names
    matches = [p for p in PARSERS if p.detect(sheets)]
    if len(matches) != 1:
        raise ValueError(f"{path.name}: no unique parser for sheets {sheets}")
    return matches[0].parse(path)
