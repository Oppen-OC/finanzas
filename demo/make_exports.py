"""Synthetic bank exports for the demo: 14 months of an invented person's
Santander and BBVA accounts, in the sheet layouts the parsers read.

Every name, merchant, amount and account number here is made up; the card
numbers are the standard 4111... test PAN. Run from the repo root:

    uv run python demo/make_exports.py     # writes data/inbox/demo-*.xlsx
    uv run python -m finanzas update       # import, check, categorize
    uv run python -m finanzas serve        # http://127.0.0.1:8000

Seeded, so the same files come out every time and re-running is a no-op import.
"""

import random
from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook

START, END = date(2025, 9, 1), date(2026, 11, 7)
INBOX = Path("data/inbox")
SAN_CARD, BBVA_CARD = "4111111111110001", "4111111111110002"
rng = random.Random(7)


def months():
    d = START
    while d <= END:
        yield d.year, d.month
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)


def eur(lo, hi):
    return round(rng.uniform(lo, hi), 2)


san, bbva = [], []   # (value date, amount EUR, ...bank specific text)


def s(d, amount, concepto):
    if d <= END:
        san.append((d, amount, concepto))


def b(d, amount, concepto, movimiento, card=True):
    if d <= END:
        obs = f"{BBVA_CARD} {concepto.upper()}" if card else ""
        bbva.append((d, amount, concepto, movimiento, obs))


def card(d, amount, merchant):
    b(d, amount, merchant, "Pago con tarjeta")


def bizum(d, amount, text):
    b(d, amount, "Bizum", ("Recibido: " if amount > 0 else "Enviado: ") + text, card=False)


def some_days(y, m, n):
    last = ((date(y, m, 28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).day
    return [date(y, m, d) for d in sorted(rng.sample(range(1, last + 1), n))]


for y, m in months():
    d = lambda day: date(y, m, day)  # noqa: E731

    # Santander: salary in, fixed costs out, and the monthly top-up of BBVA.
    s(d(1), -750.00, "Transferencia A Favor De Inmobiliaria Turia Sl Concepto: Alquiler")
    s(d(2), -900.00, "Transferencia Inmediata A Favor De Cuenta Propia Bbva Concepto: Gastos Mes")
    s(d(3), -eur(38, 72), "Recibo Iberdrola Clientes Sau")
    s(d(5), -(30.00 if (y, m) < (2026, 5) else 33.00), "Recibo Digi Spain Telecom Sl")
    s(d(6), -39.99, "Recibo Basic-fit Spain Sau")
    if m % 2:
        s(d(12), -eur(28, 40), "Recibo Emivasa Aguas De Valencia")
    for day in some_days(y, m, 2):
        s(day, -eur(35, 55), f"Compra Repsol Estacion Av Cid, Valencia, Tarjeta {SAN_CARD}")
    s(some_days(y, m, 1)[0], -eur(6, 25), f"Compra Farmacia Ruzafa, Valencia, Tarjeta {SAN_CARD}")
    s(d(27), 2150.00, "Transferencia De Acme Soluciones Sl, Concepto Nomina")
    if m in (6, 12):
        s(d(20), 2150.00, "Transferencia De Acme Soluciones Sl, Concepto Paga Extra")
    if m == 3:
        s(d(15), -186.40, "Recibo Mapfre Seguros Moto")
    if (y, m) == (2026, 6):
        s(d(24), 412.36, "Transferencia De Agencia Tributaria, Concepto Devolucion Irpf")

    # BBVA: the day to day card, Bizum, subscriptions, saving.
    card(d(1), -11.99, "Spotify")
    b(d(2), 900.00, "Transferencia recibida", "Gastos mes", card=False)
    b(d(4), -150.00, "Transferencia realizada", "Inversion indexados", card=False)
    card(d(14), -(13.99 if (y, m) < (2026, 4) else 15.99), "Netflix")
    card(d(23), -18.00, "Claude.ai Subscription")
    for day in some_days(y, m, 4):
        card(day, -eur(28, 85), "Mercadona")
    for day in some_days(y, m, rng.randint(1, 2)):
        card(day, -eur(12, 38), rng.choice(["Lidl", "Consum"]))
    for day in some_days(y, m, rng.randint(4, 8)):
        card(day, -eur(1.8, 6.5), rng.choice(["Horno El Trigal", "Cafe Mercat"]))
    for day in some_days(y, m, rng.randint(2, 4)):
        card(day, -eur(14, 48), rng.choice(["Rest. La Barraca", "Pizzeria Napoli",
                                            "Sushi Kami", "Bar El Cabanyal"]))
    for day in some_days(y, m, rng.randint(1, 2)):
        card(day, -8.90, "Cines Lys")
    if rng.random() < 0.5:
        card(some_days(y, m, 1)[0], -eur(10, 40), "Steam Purchase")
    for day in some_days(y, m, rng.randint(1, 2)):
        card(day, -eur(8, 20), "Metrovalencia")
    for day in some_days(y, m, rng.randint(1, 2)):
        card(day, -eur(12, 60), "Amazon Es")
    if rng.random() < 0.4:
        card(some_days(y, m, 1)[0], -eur(25, 70), rng.choice(["Zara", "Decathlon"]))
    if rng.random() < 0.3:
        card(some_days(y, m, 1)[0], -eur(10, 45), "Wallapop")
    if m % 2 == 0:
        card(d(9), -15.00, "Peluqueria Studio 9")
        b(d(18), -50.00, "Cajero Bbva", "Retirada de efectivo")
    day = some_days(y, m, 2)
    bizum(day[0], -eur(10, 25), rng.choice(["cena cumple", "pizza viernes", "comida domingo"]))
    bizum(day[1], eur(5, 20), rng.choice(["tu parte cena", "gasolina viaje"]))
    if rng.random() < 0.5:
        bizum(some_days(y, m, 1)[0], -eur(3, 12), rng.choice(["xd", "lo de ayer", "gracias"]))

    if m == 12:
        for day in (d(12), d(18), d(22)):
            card(day, -eur(40, 90), "El Corte Ingles")
    if m == 7:
        card(d(4), -89.00, "Fever Tickets")
    if m in (4, 8):
        card(d(10), -64.30, "Renfe Viajeros")
    if m == 9:
        card(d(15), -32.22, "Valenbisi Abono Anual")
    if (y, m) == (2026, 3):
        card(d(10), -1149.00, "Pccomponentes")
    if (y, m) == (2026, 9):
        # Four identical rows on one day: real movements, not duplicates (D2).
        for _ in range(4):
            bizum(d(15), 5.83, "regalo grupo")

# Two merchants no rule knows yet, so the demo shows the hand-off to Claude Code.
card(date(2026, 10, 21), -7.50, "Sumup *Jrm")
card(date(2026, 11, 3), -23.10, "Ccr 2 Sl")


def chained(rows, opening):
    """Oldest first with the running balance after each row, as the banks print it."""
    bal, out = round(opening * 100), []
    for row in sorted(rows, key=lambda r: r[0]):
        bal += round(row[1] * 100)
        assert bal >= 0, f"demo account overdrawn on {row[0]}"
        out.append((*row, bal / 100))
    return out


def dmy(d):
    return d.strftime("%d/%m/%Y")


def write_santander(path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Movimientos"
    rows = chained(san, 4200.00)
    for line in (["Consulta de movimientos"], ["Cuenta", "ES00 0049 0000 0000 0000 0001"],
                 ["Titular", "PERSONA DEMO"], ["Saldo", rows[-1][-1]], [],
                 ["Fecha de consulta", dmy(END)], []):
        ws.append(line)
    ws.append(["FECHA OPERACIÓN", "FECHA VALOR", "CONCEPTO", "IMPORTE EUR", "SALDO"])
    for d, amount, concepto, bal in reversed(rows):  # newest first, like the bank
        booked = d + timedelta(days=1) if concepto.startswith("Compra") else d
        ws.append([dmy(booked), dmy(d), concepto, amount, bal])
    wb.save(path)


def write_bbva(path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Informe BBVA"
    for line in (["", "Últimos movimientos"], ["", "Cuenta online"], [],
                 ["", f"Desde {dmy(START)} hasta {dmy(END)}"]):
        ws.append(line)
    ws.append(["", "F.Valor", "Fecha", "Concepto", "Movimiento", "Importe", "Divisa",
               "Disponible", "Divisa", "Observaciones"])
    for d, amount, concepto, movimiento, obs, bal in reversed(chained(bbva, 650.00)):
        booked = d + timedelta(days=1) if obs else d
        ws.append(["", d, booked, concepto, movimiento, amount, "EUR", bal, "EUR", obs])
    wb.save(path)


if __name__ == "__main__":
    INBOX.mkdir(parents=True, exist_ok=True)
    write_santander(INBOX / "demo-santander.xlsx")
    write_bbva(INBOX / "demo-bbva.xlsx")
    print(f"{len(san)} Santander and {len(bbva)} BBVA movements written to {INBOX}/")
