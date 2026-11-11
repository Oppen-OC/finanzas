"""BBVA 'Informe BBVA' export: .xlsx, no IBAN, newest first.

The only account identity in the file is the card number at the start of
Observaciones on card payments, so identifiers come from there. A file holding
only Bizum rows reveals nothing, and accounts.yaml resolves it by bank alone.
"""

import re
from pathlib import Path

import pandas as pd

from finanzas.clean import cents, iso_date, json_safe, mask
from finanzas.parsers import ParsedFile, Row, table_below

BANK = "bbva"
SHEET = "Informe BBVA"


def detect(sheet_names: list[str]) -> bool:
    return SHEET in sheet_names


def parse(path: Path) -> ParsedFile:
    sheet = pd.read_excel(path, sheet_name=SHEET, header=None, dtype=object)
    headers, body = table_below(sheet, "F.Valor")

    identifiers, rows = set(), []
    for _, r in body.iterrows():
        for col in ("Divisa", "Divisa_2"):  # constraint 7: raise, never convert
            if r[col] != "EUR":
                raise ValueError(f"{path.name}: non-EUR row ({r[col]!r}) dated {r['Fecha']}")
        obs = str(r["Observaciones"]) if isinstance(r["Observaciones"], str) else ""
        card = re.match(r"\s*(\d{16})\b", obs)
        if card:
            identifiers.add(card[1][-4:])
        # Concepto alone says just "Bizum" for every Bizum; the counterpart and the
        # message live in Movimiento, so the description needs both.
        concepto, movimiento = str(r["Concepto"]).strip(), str(r["Movimiento"]).strip()
        rows.append(Row(
            raw={h: json_safe(r[h]) for h in headers},
            date=iso_date(r["F.Valor"]),
            date_booked=iso_date(r["Fecha"]),
            amount_cents=cents(r["Importe"]),
            balance_cents=cents(r["Disponible"]),
            description=mask(f"{concepto} / {movimiento}"),
        ))
    rows.reverse()  # newest first in the file; oldest first downstream
    return ParsedFile(BANK, identifiers, rows)
