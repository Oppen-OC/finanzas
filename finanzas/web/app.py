"""The local web UI (milestone 4).

Routes only. Every figure rendered here comes from metrics.py, the same
functions `report` calls, so the dashboard and the terminal cannot disagree: the
moment a route computes a number of its own the two start to drift and the one
you are looking at is no longer the one you verified.

Bank data is read only. Nothing in this module writes to `transactions`; the
only writes it does are the inbox upload and goals.yaml, because an editable
ledger would break the raw-layer guarantee (D1).
"""

from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from finanzas import (db, goals as goals_mod, metrics as metrics_mod, projection as projection_mod,
                      review as review_mod, scenarios as scenarios_mod, update as update_mod)
from finanzas.fmt import cents as parse_cents, eur, eur_whole

HERE = Path(__file__).parent

# Relative paths in db.py, categories.py and rules.py resolve against the working
# directory, so the server has to be started from the repo root. `python -m
# finanzas serve` does that by construction; uvicorn invoked by hand does not.
app = FastAPI(title="finanzas", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.filters["eur"] = eur
# None is a real answer from the metrics (a month with no income has no savings
# rate) and must never render as 0%.
templates.env.filters["pct"] = lambda v, digits=1: "n/a" if v is None else f"{v:.{digits}%}"
templates.env.filters["signed_pct"] = lambda v: "-" if v is None else f"{v:+.0f}%"
# What goes *into* a form field: eur() groups thousands with a comma, and a form
# that cannot parse back the value it printed is a form that refuses its own
# output the first time a limit passes a thousand euros.
templates.env.filters["amount"] = lambda c: f"{c // 100}.{c % 100:02d}"
templates.env.filters["eur_whole"] = eur_whole


def heat(deviation: float) -> str:
    """The annual matrix's colour class for a cell, from the deviation
    metrics.annual_matrix computed. Presentation only: the bins are how much ink
    a departure from the row's median earns, not a number anyone reads.

    Asymmetric because the data is. Above the median is unbounded (a one-off
    purchase on a zero median is +50 or more), below it stops at -1 for an empty
    month and only goes further for a month that netted to a refund. The band
    around zero is neutral so an ordinary month is not painted at all."""
    if deviation >= 2:
        return "hu3"
    if deviation >= 0.75:
        return "hu2"
    if deviation >= 0.25:
        return "hu1"
    if deviation <= -1:
        return "hd3"
    if deviation <= -0.6:
        return "hd2"
    if deviation <= -0.25:
        return "hd1"
    return ""


templates.env.filters["heat"] = heat


def connection():
    """One connection per request: sqlite3 connections are not shared across
    threads and FastAPI runs sync endpoints in a threadpool."""
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


# The page renders every matching row rather than paginating: four years is a
# few thousand rows, which a browser draws instantly from localhost, and paging
# is a control, a state and an off-by-one for a problem this data does not have.
# The cap exists only so a pathological filter cannot hang the tab.
MAX_ROWS = 2000


# A real Santander export is about 50 KB. The cap is not a guess at what a bank
# might send, it is a bound on what a mistyped upload can write to disk.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def save_upload(name: str, data: bytes) -> tuple[Path, str]:
    """Write an uploaded export into the inbox, never over an existing file.

    Banks name their exports generically, so a collision is the normal case, not
    the exotic one. An identical file is left alone (import skips it by hash
    anyway); a different file with the same name is saved beside it under a free
    name, because overwriting would destroy a source export and the raw layer's
    whole point is that a parsing decision can be replayed later (D1)."""
    # Only the basename: a filename is client-supplied, and "../../accounts.yaml"
    # is a legal string to put in a multipart part.
    name = Path(name).name
    target = update_mod.INBOX / name
    if target.exists():
        if target.read_bytes() == data:
            return target, "ya estaba en data/inbox, sin cambios"
        stem, suffix, n = target.stem, target.suffix, 2
        while target.exists():
            target = update_mod.INBOX / f"{stem} ({n}){suffix}"
            n += 1
        return target, f"otro fichero se llamaba igual, guardado como {target.name}"
    return target, ""


def category_options(categories):
    """The taxonomy as optgroups: a top level category, then its children. The
    parent is itself an option, because filtering by `salidas` has to include
    `salidas-cafes` or the filter is lying about what it selected."""
    return [(c, [k for k in categories.values() if k.parent == c.id])
            for c in categories.values() if c.parent is None]


def months_with_data(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(date, 1, 7) FROM transactions ORDER BY 1 DESC")]


@app.get("/importar", response_class=HTMLResponse)
def import_form(request: Request):
    return templates.TemplateResponse(request, "import.html", {"page": "importar"})


@app.post("/importar", response_class=HTMLResponse)
def import_upload(request: Request, files: list[UploadFile] = [], conn=Depends(connection)):
    """Save the uploaded exports into data/inbox and run the same round as
    `finanzas update`: import, check, recategorize, then name what no rule
    matched. Importing without recategorizing would leave the new rows
    uncategorized and every category on the dashboard quietly short until
    someone remembered to run a second command.

    This is the one endpoint that writes. It writes files into the inbox and
    inserts transactions, both through the same code path as the CLI; it never
    updates or deletes an existing row (C5)."""
    saved, rejected = [], []
    for upload in files:
        name = Path(upload.filename or "").name
        if not name:
            continue
        if not name.lower().endswith(update_mod.SUFFIXES):
            rejected.append({"file": name, "why": "no es un .xls ni un .xlsx"})
            continue
        data = upload.file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            rejected.append({"file": name, "why": f"pasa de {MAX_UPLOAD_BYTES // 1024 // 1024} MB"})
            continue
        if not data:
            rejected.append({"file": name, "why": "llega vacío"})
            continue
        path, note = save_upload(name, data)
        if not path.exists() or path.read_bytes() != data:
            update_mod.INBOX.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        saved.append({"file": path.name, "note": note, "path": path})

    context = {"page": "importar", "rejected": rejected,
               "uncategorized_id": metrics_mod.UNCATEGORIZED,
               "saved": [{k: v for k, v in s.items() if k != "path"} for s in saved]}
    if saved:
        # Only the uploaded files are imported; the check and the recategorization
        # look at the whole ledger, because a new export can break a chain that
        # was whole before it arrived.
        context["result"] = update_mod.run(conn, [s["path"] for s in saved])
    return templates.TemplateResponse(request, "import.html", context)


@app.get("/movimientos", response_class=HTMLResponse)
def transactions(request: Request, month: str | None = None, account: str | None = None,
                 category: str | None = None, q: str | None = None, conn=Depends(connection)):
    """Read only, on purpose. An editable ledger would break the raw layer
    guarantee (D1): a category is fixed by writing a rule or a manual.yaml entry,
    not by typing over the row, so the files still determine the whole ledger.

    The filters compose with AND and each one is optional; the query itself lives
    in metrics.find_transactions, shared with `list`."""
    cats = metrics_mod.load_categories()
    # One row past the cap, so "there are more" is a fact rather than a guess.
    rows = metrics_mod.find_transactions(
        conn, account=account or None, month=month or None, search=q or None,
        category=category or None, limit=MAX_ROWS + 1)
    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]

    context = {
        "page": "movimientos",
        "rows": rows,
        "categories": cats,
        "truncated": truncated,
        "max_rows": MAX_ROWS,
        "net_cents": sum(r["amount_cents"] for r in rows),
        "months": months_with_data(conn),
        "accounts": [r[0] for r in conn.execute(
            "SELECT DISTINCT account FROM transactions ORDER BY 1")],
        "category_options": category_options(cats),
        "uncategorized": metrics_mod.UNCATEGORIZED,
        "f": {"month": month or "", "account": account or "",
              "category": category or "", "q": q or ""},
    }
    # HTMX asks for the results block alone; a browser without JS, a reload or a
    # shared link gets the whole page from the same route and the same context.
    template = "_results.html" if request.headers.get("hx-request") else "transactions.html"
    return templates.TemplateResponse(request, template, context)


def amount(raw: str, what: str) -> int:
    """A euro amount as typed by a human, in cents. Accepts the decimal comma,
    since the page is in Spanish and the keyboard's comma is what a Spanish
    keypad gives you; refuses a thousands separator rather than guessing which
    of the two symbols meant what."""
    value = raw.strip().replace(" ", "").replace(",", ".")
    try:
        return parse_cents(value, what)
    except Exception:
        raise ValueError(f"«{raw}» no es un importe. Escribe los euros con punto o coma "
                         "decimal y sin separador de miles: 350 o 350,50.")


def goals_context(conn, error: str | None = None) -> dict:
    cats = metrics_mod.load_categories()
    goals = goals_mod.load(categories=cats)
    # The month a change starts from is today's, the one place the wall clock is
    # the right clock: setting a limit is a decision made today, while every
    # measurement is pinned to the end of the ledger instead.
    month = date.today().isoformat()[:7]
    return {
        "page": "objetivos",
        "error": error,
        "month": month,
        "goals": goals,
        "categories": cats,
        "in_force": sorted(goals.at(month).items(), key=lambda kv: -kv[1].monthly_cents),
        "category_options": category_options(cats),
        "pace_by_category": {line["category"]: line
                             for line in metrics_mod.month_to_date(conn, month, goals, cats)["lines"]},
        "pace_liquid": metrics_mod.liquid_balance(conn)["total_cents"],
        "path": str(goals_mod.GOALS_PATH),
    }


@app.get("/objetivos", response_class=HTMLResponse)
def goals_form(request: Request, conn=Depends(connection)):
    return templates.TemplateResponse(request, "goals.html", goals_context(conn))


@app.post("/objetivos", response_class=HTMLResponse)
def goals_save(request: Request, do: str = Form(...), category: str = Form(""),
               monthly_eur: str = Form(""), liquid_eur: str = Form(""),
               conn=Depends(connection)):
    """The only editable thing in the UI, and it writes a file rather than a row
    (see goals.py). A bad number or an unknown category is a mistyped form, not
    a server fault, so it comes back as the page with a message on it.

    Post/redirect/get: a reload of this page must not set the limit again, and
    with `since` fixed to the current month a resubmission would be invisible
    rather than harmless."""
    since = date.today().isoformat()[:7]
    try:
        if do == "liquid":
            goals_mod.set_liquid(amount(liquid_eur, "saldo") if liquid_eur.strip() else None)
        elif do == "unset":
            goals_mod.unset_limit(category, since)
        elif do == "set":
            if not monthly_eur.strip():
                raise ValueError("Escribe un importe, o usa Quitar para levantar el límite.")
            goals_mod.set_limit(category, amount(monthly_eur, "límite"), since)
        else:
            raise ValueError(f"acción desconocida: {do!r}")
    except ValueError as e:
        return templates.TemplateResponse(request, "goals.html", goals_context(conn, str(e)))
    return RedirectResponse("/objetivos", status_code=303)


@app.get("/revisiones", response_class=HTMLResponse)
def reviews(request: Request, month: str | None = None, conn=Depends(connection)):
    """Every complete month's score, and the selected month's review in full: the
    components and talking points from review.py and the text `/asesor` wrote.

    The score is review.month_review's, the same call `finanzas review` makes, so
    this page and the terminal cannot disagree about a month (D22, D29). A month
    with no written review still shows its score: the number exists as soon as the
    month is complete, the commentary only once `/asesor` has been run for it."""
    done = metrics_mod.complete_months(conn)
    if not done:
        return templates.TemplateResponse(request, "reviews.html",
                                          {"page": "revisiones", "reviews": []})
    cats = metrics_mod.load_categories()
    goals = goals_mod.load(categories=cats)
    # Newest first, like the dashboard's month picker.
    all_reviews = [review_mod.month_review(conn, m, cats, goals) for m in reversed(done)]
    by_month = {r["month"]: r for r in all_reviews}
    # Same fallback as the dashboard: a stale or partial ?month shows the latest
    # complete month rather than a 404. A partial month has no review to show.
    month = month if month in by_month else all_reviews[0]["month"]
    text = review_mod.written(month)
    return templates.TemplateResponse(request, "reviews.html", {
        "page": "revisiones",
        "reviews": all_reviews,
        "months": [r["month"] for r in all_reviews],
        "month": month,
        "r": by_month[month],
        "text": text,
        "text_score": review_mod.written_score(text) if text else None,
        "has_text": {m: (review_mod.REVIEWS_DIR / f"{m}.md").exists() for m in by_month},
        "opaque_warn": review_mod.OPAQUE_WARN_SHARE,
    })


@app.get("/anual", response_class=HTMLResponse)
def annual(request: Request, end: str | None = None, conn=Depends(connection)):
    """Category x month over the last twelve complete months (D30). The dashboard
    is one month at a time, and nothing else on the site shows the year: which
    costs are monthly habits, which are annual charges, which month a spike
    landed in. The matrix is annual_matrix's, cell for cell; this route only
    picks the window, and an unknown ?end falls back inside annual_matrix."""
    matrix = metrics_mod.annual_matrix(conn, end or None)
    return templates.TemplateResponse(request, "annual.html", {
        "page": "anual",
        "m": matrix,
        # Newest first, like every other month picker on the site.
        "ends": list(reversed(metrics_mod.complete_months(conn))),
        "uncategorized": metrics_mod.UNCATEGORIZED,
        "min_baseline_months": metrics_mod.MIN_BASELINE_MONTHS,
        "floor_cents": metrics_mod.DEVIATION_FLOOR_CENTS,
    })


@app.get("/escenarios", response_class=HTMLResponse)
def scenarios_page(request: Request, s: str | None = None, conn=Depends(connection)):
    """The seventh page (D31): the balance month by month under each scenario in
    scenarios.yaml, and when each one crosses the floor and zero. Read only; the
    file is edited by hand in this iteration.

    Everything comes from projection.project_all, the call `finanzas project`
    makes. The one thing done here is dividing by 100 for the chart, as the
    dashboard does. A missing file is a page that says how to create one, and a
    malformed one is a page naming the scenario and event, never a 500."""
    context = {"page": "escenarios", "path": str(scenarios_mod.SCENARIOS_PATH),
               "error": None, "res": None}
    cats = metrics_mod.load_categories()
    try:
        config = scenarios_mod.load(categories=cats)
    except ValueError as e:
        context["error"] = str(e)
        return templates.TemplateResponse(request, "scenarios.html", context)
    if config is None:
        return templates.TemplateResponse(request, "scenarios.html", context)

    res = projection_mod.project_all(conn, config, cats)
    context["res"] = res
    if res["start"] is None:
        return templates.TemplateResponse(request, "scenarios.html", context)
    by_id = {sc["id"]: sc for sc in res["scenarios"]}
    # Same fallback as every other picker on the site: an unknown ?s shows the
    # first scenario rather than a 404.
    selected = s if s in by_id else res["scenarios"][0]["id"]
    st = res["start"]
    context.update({
        "sel": by_id[selected],
        "min_baseline_months": metrics_mod.MIN_BASELINE_MONTHS,
        # Every line starts at the same point, the balance at the ledger's end,
        # so the chart reads as where the scenarios diverge from.
        "chart": {
            "labels": [st["as_of"]] + [r["month"] + ("*" if r["partial"] else "")
                                       for r in res["scenarios"][0]["rows"]],
            "floor": res["floor_cents"] / 100,
            "series": [{"id": sc["id"], "name": sc["name"],
                        "values": [st["liquid_cents"] / 100]
                                  + [r["closing_cents"] / 100 for r in sc["rows"]]}
                       for sc in res["scenarios"]],
        },
    })
    return templates.TemplateResponse(request, "scenarios.html", context)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, month: str | None = None, conn=Depends(connection)):
    months = months_with_data(conn)
    if not months:
        return templates.TemplateResponse(request, "dashboard.html",
                                          {"page": "panel", "months": []})
    # An unknown or malformed ?month falls back to the latest rather than 404ing:
    # there is one user and a stale bookmark is not an error worth a page for.
    month = month if month in months else months[0]

    cats = metrics_mod.load_categories()
    summary = metrics_mod.monthly_summary(conn, month, cats)
    split = metrics_mod.fixed_vs_variable(conn, month, cats)
    saving = metrics_mod.savings_rate(conn, month, cats)
    disc = metrics_mod.discretionary_share(conn, month, cats)
    burn = metrics_mod.burn_rate(conn, cats)
    run = metrics_mod.runway(conn, None, cats)
    recurring = metrics_mod.find_recurring(conn, cats)
    live = [r for r in recurring if r["active"]]
    pace = metrics_mod.month_to_date(conn, month, None, cats)

    # Chart payloads carry euros, not cents: the axis is read by a human and
    # Chart.js would otherwise need a divide-by-100 in every formatter.
    # The trend plots complete months only (D16). A partial month drawn as a
    # low point is the exact lie the coverage logic exists to prevent, and a
    # chart is read faster than the caption that would qualify it.
    trend = {
        "labels": [m for m, _ in burn["samples"]],
        "values": [v / 100 for _, v in burn["samples"]],
        "median": burn["median_cents"] / 100 if burn["median_cents"] is not None else None,
    }

    return templates.TemplateResponse(request, "dashboard.html", {
        "page": "panel",
        "months": months,
        "month": month,
        "coverage": metrics_mod.coverage(conn),
        "complete_months": metrics_mod.complete_months(conn),
        "summary": summary,
        "split": split,
        "saving": saving,
        "disc": disc,
        "burn": burn,
        "run": run,
        "recurring": recurring,
        "pace": pace,
        "live_annual_cents": sum(r["annual_cents"] for r in live),
        "n_live": len(live),
        "trend_chart": trend,
        "min_baseline_months": metrics_mod.MIN_BASELINE_MONTHS,
        # The breakdown links through to the transactions page, so it needs
        # the sentinel the uncategorized row filters on.
        "uncategorized": metrics_mod.UNCATEGORIZED,
    })
