"""Helpers shared by every parser: masking, money, dates, text normalization."""

import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal

# D6: any long digit run (card PANs, IBAN bodies, SEPA mandate refs) is cut to its
# last 4 before anything is persisted. {12,} and not {12,19}: a 22-digit IBAN body
# would otherwise leak its last 3 digits past a 19-digit match.
_LONG_DIGITS = re.compile(r"\d{12,}")
# Card numbers written in 4-digit groups ("4111 1111 1111 1111").
_GROUPED_PAN = re.compile(r"\b\d{4}(?:[ -]\d{4}){2,3}\b")
# Santander prints a partial PAN as "Tarj. :*111111"; six digits is more than we need.
_PARTIAL_CARD = re.compile(r"\*(\d{5,})")


def mask(text: str) -> str:
    text = _GROUPED_PAN.sub(lambda m: "*" + re.sub(r"\D", "", m[0])[-4:], text)
    text = _LONG_DIGITS.sub(lambda m: "*" + m[0][-4:], text)
    return _PARTIAL_CARD.sub(lambda m: "*" + m[1][-4:], text)


def has_long_digits(text: str) -> bool:
    return bool(_LONG_DIGITS.search(text) or _GROUPED_PAN.search(text))


def cents(value) -> int:
    """Excel number -> integer cents. Refuses anything it would have to guess at."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"expected a numeric amount, got {value!r}")
    # repr() of a float is its shortest round-tripping decimal, so 4815.1 stays
    # 4815.1 instead of 4815.09999... before scaling.
    d = Decimal(repr(value)) * 100
    if d != d.to_integral_value():
        raise ValueError(f"amount {value!r} has sub-cent precision")
    return int(d)


def iso_date(value) -> str:
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    return datetime.strptime(str(value).strip(), "%d/%m/%Y").strftime("%Y-%m-%d")


def normalize(text: str) -> str:
    """Lowercase, accent-free, single-spaced. The form categorization rules match on."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip().lower()


def json_safe(value):
    """Cell value for raw_rows: masked strings, numbers as-is, blanks as None."""
    if value is None or (isinstance(value, float) and value != value):
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, str):
        return mask(value)
    return value
