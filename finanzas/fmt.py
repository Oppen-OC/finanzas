"""Money, in one place: cents to a string, and euros as written by a human back
to cents.

Lives outside cli.py because the web UI needs the same functions: two formatters
drift, and a dashboard that rounds differently from the terminal is a bug report
waiting to happen. The parser is here for the same reason, shared by rules.yaml
and goals.yaml, which both let a human write euros.
"""

from decimal import Decimal


def eur(c: int) -> str:
    """Integer cents as a euro string. The sign goes in front of the thousands
    separator, not inside the number: -1234567 is -12,345.67, not -12,-345.67."""
    sign = "-" if c < 0 else ""
    return f"{sign}{abs(c) // 100:,}.{abs(c) % 100:02d}"


def eur_whole(c: int) -> str:
    """Whole euros, rounded half away from zero, for the annual matrix: twelve
    columns of "1,234.56" do not fit a screen, and at a year's scale the cents
    are noise. Totals elsewhere keep eur(). Rounds the magnitude, so -0.50 and
    0.50 land symmetrically on -1 and 1."""
    sign = "-" if c < 0 else ""
    whole = (abs(c) + 50) // 100
    return f"{sign if whole else ''}{whole:,}"


def cents(value, where: str) -> int:
    """Euros as written in a YAML file, as integer cents (C8).

    Through Decimal, not float: 19.99 * 100 is 1998.9999999999998 in binary
    floating point, and a limit silently a cent low is the kind of bug nobody
    ever goes looking for."""
    d = Decimal(str(value)) * 100
    if d != d.to_integral_value():
        raise ValueError(f"{where}: amount {value!r} is not a whole number of cents")
    return int(d)
