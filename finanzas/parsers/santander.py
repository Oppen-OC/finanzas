"""Santander 'Movimientos' export: legacy .xls, preamble with IBAN, newest first."""

import re
from pathlib import Path

import pandas as pd

from finanzas.clean import cents, iso_date, json_safe, mask
from finanzas.parsers import ParsedFile, Row, table_below

BANK = "santander"
SHEET = "Movimientos"


def detect(sheet_names: list[str]) -> bool:
    return SHEET in sheet_names


def parse(path: Path) -> ParsedFile:
    sheet = pd.read_excel(path, sheet_name=SHEET, header=None, dtype=object)
    headers, body = table_below(sheet, "FECHA OPERACIÓN")
    if "IMPORTE EUR" not in headers:  # constraint 7: the currency lives in the header
        raise ValueError(f"{path.name}: expected 'IMPORTE EUR' column, got {headers}")

    # The IBAN only appears in the preamble. Keep its last 4 as the account identifier;
    # the preamble itself is never stored.
    preamble = " ".join(str(c) for c in sheet.iloc[:10].to_numpy().ravel() if isinstance(c, str))
    iban = re.search(r"\bES\d{2}(?:\s?\d{4}){5}\b", preamble)
    identifiers = {re.sub(r"\D", "", iban[0])[-4:]} if iban else set()

    rows = []
    for _, r in body.iterrows():
        rows.append(Row(
            raw={h: json_safe(r[h]) for h in headers},
            date=iso_date(r["FECHA VALOR"]),
            date_booked=iso_date(r["FECHA OPERACIÓN"]),
            amount_cents=cents(r["IMPORTE EUR"]),
            balance_cents=cents(r["SALDO"]),
            description=mask(str(r["CONCEPTO"]).strip()),
        ))
    rows.reverse()  # file is newest first; everything downstream works oldest first
    return ParsedFile(BANK, identifiers, rows)
