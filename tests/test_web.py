"""The web UI's own guarantees. What it must never do is compute a number: the
properties here are that it renders what metrics.py returns, that the first run
against an empty ledger is a page and not a traceback, and that nothing on the
page reaches the network."""

import re
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from finanzas import (db, goals as goals_mod, metrics as metrics_mod, projection as projection_mod,
                      review as review_mod, scenarios as scenarios_mod, update as update_mod)
from finanzas.fmt import eur
from finanzas.web import app as webapp

REAL_BBVA = next(Path("data/inbox").glob("*.xlsx"), None)


class WebBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.db"
        self.conn = db.connect(self.path)
        self.conn.execute("INSERT INTO imports (id, sha256, filename, bank, account, imported_at, "
                          "n_rows, n_inserted, n_duplicate) VALUES (1,'x','x.xls','bbva','bbva',"
                          "'2026-09-01T00:00:00',0,0,0)")
        self.conn.execute("INSERT INTO raw_rows (id, import_id, position, data) VALUES (1,1,0,'{}')")
        # Commit before the app opens its own connection: an uncommitted write
        # here holds the lock and every request fails with "database is locked".
        self.conn.commit()
        self.n = 0
        # The app opens its own connection per request, so it has to be pointed
        # at the fixture through the same module-level path the CLI reads.
        self._real_db = db.DB_PATH
        db.DB_PATH = self.path
        # Same treatment for goals.yaml: the dashboard reads it now, and the
        # Objetivos page writes it. A test that touched the real file would both
        # depend on whatever limits are set today and destroy them.
        self._real_goals = goals_mod.GOALS_PATH
        goals_mod.GOALS_PATH = Path(self.tmp.name) / "goals.yaml"
        self.client = TestClient(webapp.app)

    def tearDown(self):
        db.DB_PATH = self._real_db
        goals_mod.GOALS_PATH = self._real_goals
        self.conn.close()
        self.tmp.cleanup()

    def add(self, date, amount_cents, description="x", category=None):
        self.n += 1
        self.conn.execute(
            "INSERT INTO transactions (account, date, date_booked, amount_cents, balance_cents, "
            "occurrence, description, description_norm, raw_row_id, category, category_src) "
            "VALUES ('bbva',?,?,?,?,?,?,?,1,?,'rule:t')",
            (date, date, amount_cents, self.n * 1000, self.n, description, description, category))
        self.conn.commit()


class EmptyLedger(WebBase):
    def test_first_run_renders_a_page_not_a_traceback(self):
        # The state every new install starts in. Every metric returns None or
        # an empty list here, and a dashboard that divides by one of them 500s
        # on the first thing the user ever does.
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("data/inbox", r.text)


class Dashboard(WebBase):
    def setUp(self):
        super().setUp()
        for month in ("2026-06", "2026-07", "2026-08"):
            self.add(f"{month}-01", 200000, "nomina", "ingresos")
            self.add(f"{month}-05", -30000, "mercadona", "supermercado")
            self.add(f"{month}-28", -1800, "anthropic", "suscripciones")

    def test_renders_the_figures_metrics_returns(self):
        r = self.client.get("/?month=2026-07")
        self.assertEqual(r.status_code, 200)
        self.assertIn(eur(200000), r.text)   # income
        self.assertIn(eur(31800), r.text)    # spend
        self.assertIn("2026-07", r.text)

    def test_unknown_month_falls_back_to_the_latest(self):
        # A stale bookmark or a hand-typed month is not worth a 404 page for a
        # single user; it is worth not silently rendering an empty month.
        r = self.client.get("/?month=1999-13")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Panel · 2026-08", r.text)

    def test_partial_month_is_labelled(self):
        # August is the last month in the fixture and stops on the 28th, so it
        # is not covered end to end. Reporting it without the banner is how a
        # short month reads as a cheap one.
        r = self.client.get("/?month=2026-08")
        self.assertIn("mes parcial", r.text)

    def test_nothing_on_the_page_is_loaded_over_the_network(self):
        # Constraint 1. A CDN script tag would make the dashboard depend on an
        # external service and announce every open to it.
        r = self.client.get("/")
        for url in re.findall(r'(?:src|href)="([^"]+)"', r.text):
            self.assertFalse(url.startswith(("http://", "https://", "//")), url)

    def test_chart_js_is_actually_served(self):
        # The vendored file being referenced is not the same as it being there.
        r = self.client.get("/static/chart.umd.js")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Chart.js v4", r.text[:200])


class NoCompleteMonthYet(WebBase):
    def test_a_ledger_with_no_complete_month_still_renders(self):
        # The state right after the first import: there is data, but no month is
        # covered end to end, so burn_rate has nothing to take a median of and
        # returns None. The dashboard used to 500 here on eur(None).
        self.add("2026-08-03", -3000, "mercadona", "supermercado")
        self.add("2026-08-04", -400, "bizum", None)
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("sin meses completos todavía", r.text)
        self.assertIn("no hay nada", r.text)


class Transactions(WebBase):
    def setUp(self):
        super().setUp()
        self.add("2026-07-02", -2000, "cafe bali", "salidas-cafes")
        self.add("2026-08-03", -3000, "mercadona centro", "supermercado")
        self.add("2026-08-04", -400, "bizum orxata", None)

    def test_filters_compose(self):
        r = self.client.get("/movimientos?month=2026-08&q=mercadona")
        self.assertIn("mercadona centro", r.text)
        self.assertNotIn("bizum orxata", r.text)
        self.assertNotIn("cafe bali", r.text)

    def test_a_parent_category_includes_its_children(self):
        r = self.client.get("/movimientos?category=salidas")
        self.assertIn("cafe bali", r.text)
        self.assertNotIn("mercadona centro", r.text)

    def test_uncategorized_filter(self):
        r = self.client.get("/movimientos?category=__uncategorized__")
        self.assertIn("bizum orxata", r.text)
        self.assertNotIn("mercadona centro", r.text)

    def test_htmx_gets_the_results_block_and_a_browser_gets_the_page(self):
        frag = self.client.get("/movimientos", headers={"HX-Request": "true"})
        page = self.client.get("/movimientos")
        self.assertTrue(frag.text.lstrip().startswith('<div id="resultados">'))
        self.assertNotIn("<html", frag.text)
        self.assertIn("<html", page.text)

    def test_the_form_still_works_without_javascript(self):
        # HTMX removes the reload; it is not what makes the filters work. If the
        # form ever loses its action or method, a no-JS visitor gets a dead page.
        r = self.client.get("/movimientos")
        self.assertIn('action="/movimientos" method="get"', r.text)

    def test_the_page_never_offers_to_write(self):
        # Bank data is read only (D1). A POST target on this page would be the
        # first crack in the raw-layer guarantee.
        for url in ("/", "/movimientos"):
            self.assertNotIn('method="post"', self.client.get(url).text.lower())

    def test_htmx_is_served_locally(self):
        r = self.client.get("/static/htmx.min.js")
        self.assertEqual(r.status_code, 200)
        self.assertIn("htmx", r.text[:200])

    def test_the_dashboard_links_through_to_this_page(self):
        r = self.client.get("/?month=2026-08")
        self.assertIn("/movimientos?month=2026-08&category=supermercado", r.text)


class Upload(WebBase):
    """The one endpoint that writes. What it must never do is lose a source file
    or put one somewhere other than the inbox."""

    def setUp(self):
        super().setUp()
        self.inbox = Path(self.tmp.name) / "inbox"
        self.inbox.mkdir()
        self._real_inbox = update_mod.INBOX
        update_mod.INBOX = self.inbox

    def tearDown(self):
        update_mod.INBOX = self._real_inbox
        super().tearDown()

    def post(self, name, data=b"not really a spreadsheet"):
        return self.client.post("/importar", files=[("files", (name, data))])

    def files_in_inbox(self):
        return sorted(p.name for p in self.inbox.iterdir())

    def test_the_form_renders(self):
        r = self.client.get("/importar")
        self.assertEqual(r.status_code, 200)
        self.assertIn('enctype="multipart/form-data"', r.text)

    def test_a_file_that_is_not_an_export_is_refused_and_not_written(self):
        r = self.post("notas.txt")
        self.assertIn("no es un .xls ni un .xlsx", r.text)
        self.assertEqual(self.files_in_inbox(), [])

    def test_an_empty_file_is_refused(self):
        self.assertIn("llega vacío", self.post("vacio.xlsx", b"").text)
        self.assertEqual(self.files_in_inbox(), [])

    def test_a_filename_cannot_escape_the_inbox(self):
        # A filename is client supplied and "../../accounts.yaml" is a legal
        # string to put in a multipart part. Only the basename is ever used.
        self.post("../../pwned.xlsx", b"x")
        self.assertEqual(self.files_in_inbox(), ["pwned.xlsx"])
        self.assertFalse((self.inbox.parent.parent / "pwned.xlsx").exists())

    def test_a_name_collision_never_overwrites_a_source_file(self):
        # Banks name exports generically, so this is the normal case. Overwriting
        # would destroy an export the raw layer exists to be able to replay (D1).
        (self.inbox / "movimientos.xlsx").write_bytes(b"first export")
        self.post("movimientos.xlsx", b"a different export")
        self.assertEqual((self.inbox / "movimientos.xlsx").read_bytes(), b"first export")
        self.assertEqual(self.files_in_inbox(), ["movimientos (2).xlsx", "movimientos.xlsx"])

    def test_an_identical_file_is_not_stored_twice(self):
        (self.inbox / "movimientos.xlsx").write_bytes(b"same bytes")
        self.post("movimientos.xlsx", b"same bytes")
        self.assertEqual(self.files_in_inbox(), ["movimientos.xlsx"])

    @unittest.skipIf(REAL_BBVA is None, "no BBVA export in data/inbox")
    def test_a_real_export_imports_checks_and_categorizes_in_one_post(self):
        # Importing without recategorizing would leave the new rows uncategorized
        # and every category on the dashboard short until someone ran a second
        # command, so the round the page runs has to be the whole round.
        r = self.post(REAL_BBVA.name, REAL_BBVA.read_bytes())
        self.assertEqual(r.status_code, 200)
        n, uncategorized = self.conn.execute(
            "SELECT COUNT(*), SUM(category IS NULL) FROM transactions").fetchone()
        self.assertGreater(n, 0)
        self.assertLess(uncategorized, n)          # categorize ran
        self.assertIn("rotura(s) de cadena", r.text)   # the check ran
        self.assertIn("por regla", r.text)

        # And the same file a second time is recognised by its hash, not re-read.
        again = self.post(REAL_BBVA.name, REAL_BBVA.read_bytes())
        self.assertIn("skipped (already imported)", again.text)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], n)


class Goals(WebBase):
    """The only editable page. It writes goals.yaml, not a row, and a mistyped
    amount is a message on the page rather than a traceback."""

    def setUp(self):
        super().setUp()
        self.add("2026-08-03", -30000, "mercadona", "supermercado")
        self.add("2026-08-04", -20000, "cafe", "salidas-cafes")

    def limits(self):
        return goals_mod.load().at(webapp.date.today().isoformat()[:7])

    def test_empty_page_renders_and_offers_the_form(self):
        r = self.client.get("/objetivos")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Todavía no hay ningún límite", r.text)

    def test_setting_a_limit_writes_the_file_and_redirects(self):
        r = self.client.post("/objetivos",
                             data={"do": "set", "category": "supermercado", "monthly_eur": "350"})
        self.assertEqual(r.status_code, 200)             # followed the redirect
        self.assertEqual(str(r.url).endswith("/objetivos"), True)
        self.assertEqual(self.limits()["supermercado"].monthly_cents, 35000)
        self.assertIn("350.00", self.client.get("/objetivos").text)

    def test_a_decimal_comma_is_accepted(self):
        self.client.post("/objetivos",
                         data={"do": "set", "category": "supermercado", "monthly_eur": "350,50"})
        self.assertEqual(self.limits()["supermercado"].monthly_cents, 35050)

    def test_a_mistyped_amount_is_a_message_not_a_traceback(self):
        r = self.client.post("/objetivos",
                             data={"do": "set", "category": "supermercado", "monthly_eur": "mucho"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("no es un importe", r.text)
        self.assertEqual(self.limits(), {})

    def test_an_unknown_category_is_refused(self):
        r = self.client.post("/objetivos",
                             data={"do": "set", "category": "supermecado", "monthly_eur": "350"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("No se ha guardado", r.text)
        self.assertEqual(self.limits(), {})

    def test_removing_a_limit(self):
        self.client.post("/objetivos",
                         data={"do": "set", "category": "supermercado", "monthly_eur": "350"})
        self.client.post("/objetivos", data={"do": "unset", "category": "supermercado"})
        self.assertEqual(self.limits(), {})

    def test_the_liquid_override_reaches_the_runway(self):
        self.client.post("/objetivos", data={"do": "liquid", "liquid_eur": "3000"})
        self.assertEqual(goals_mod.load().liquid_cents, 300000)
        self.assertIn(eur(300000), self.client.get("/").text)
        # And an empty field puts it back on the sum of the account balances.
        self.client.post("/objetivos", data={"do": "liquid", "liquid_eur": ""})
        self.assertIsNone(goals_mod.load().liquid_cents)


class Pace(WebBase):
    def setUp(self):
        super().setUp()
        self.add("2026-08-01", -20000, "mercadona", "supermercado")
        self.add("2026-08-15", -10000, "mercadona", "supermercado")

    def test_without_limits_the_dashboard_says_where_to_set_them(self):
        r = self.client.get("/?month=2026-08")
        self.assertIn("Sin límites todavía", r.text)
        self.assertIn('href="/objetivos"', r.text)

    def test_the_page_renders_exactly_what_month_to_date_returns(self):
        # The route must not compute a number of its own (D22): every figure in
        # the block is asserted against the function the CLI prints from.
        goals_mod.set_limit("supermercado", 40000, "2026-01")
        pace = metrics_mod.month_to_date(self.conn, "2026-08")
        r = self.client.get("/?month=2026-08")
        line = pace["lines"][0]
        self.assertEqual(line["spent_cents"], 30000)
        self.assertEqual(line["projected_cents"], round(30000 * 31 / 15))
        for value in (line["spent_cents"], line["limit_cents"], line["projected_cents"]):
            self.assertIn(eur(value), r.text)
        self.assertIn("va camino de pasarse", r.text)
        self.assertIn(f"{pace['days_elapsed']} de {pace['days_in_month']}", r.text)


class Reviews(WebBase):
    """The Revisiones page shows review.month_review's score and nothing it
    computed itself, and a month with no written review is still a page."""

    def setUp(self):
        super().setUp()
        # A consistent running balance: the review refuses to score a ledger whose
        # chain breaks, and WebBase.add writes balances that do not chain.
        balance = 0
        rows = [("2025-12-31", 1_000_000, "nomina", "ingresos")]
        for month in ("2026-01", "2026-02", "2026-03", "2026-04", "2026-05"):
            rows += [(f"{month}-05", -30000, "mercadona", "supermercado"),
                     (f"{month}-28", -1800, "anthropic", "suscripciones")]
        rows.append(("2026-06-02", -500, "mercadona", "supermercado"))
        for i, (d, amount, desc, cat) in enumerate(rows, 1):
            balance += amount
            self.conn.execute(
                "INSERT INTO transactions (account, date, date_booked, amount_cents, "
                "balance_cents, occurrence, description, description_norm, raw_row_id, "
                "category, category_src) VALUES ('bbva',?,?,?,?,?,?,?,1,?,'rule:t')",
                (d, d, amount, balance, i, desc, desc, cat))
        self.conn.commit()
        self._real_reviews = review_mod.REVIEWS_DIR
        review_mod.REVIEWS_DIR = Path(self.tmp.name) / "reviews"

    def tearDown(self):
        review_mod.REVIEWS_DIR = self._real_reviews
        super().tearDown()

    def test_score_on_the_page_is_month_reviews(self):
        expected = review_mod.month_review(self.conn, "2026-04")["score"]
        self.assertIsNotNone(expected)
        r = self.client.get("/revisiones?month=2026-04")
        self.assertEqual(r.status_code, 200)
        self.assertIn(f"{expected:.1f}", r.text)
        self.assertIn("/asesor 2026-04", r.text)       # no written review yet

    def test_written_review_is_shown_escaped(self):
        review_mod.REVIEWS_DIR.mkdir()
        (review_mod.REVIEWS_DIR / "2026-05.md").write_text(
            "# Review 2026-05\n<script>x</script>", encoding="utf-8")
        r = self.client.get("/revisiones")                 # defaults to the latest complete month
        self.assertIn("# Review 2026-05", r.text)
        self.assertNotIn("<script>x</script>", r.text)

    def test_a_review_whose_score_no_longer_matches_is_flagged(self):
        review_mod.REVIEWS_DIR.mkdir()
        (review_mod.REVIEWS_DIR / "2026-04.md").write_text(
            "# Review 2026-04 · score 0.1/10\n", encoding="utf-8")
        self.assertIn("desfasada", self.client.get("/revisiones?month=2026-04").text)
        score = review_mod.month_review(self.conn, "2026-04")["score"]
        (review_mod.REVIEWS_DIR / "2026-04.md").write_text(
            f"# Review 2026-04 · score {score:.1f}/10\n", encoding="utf-8")
        self.assertNotIn("desfasada", self.client.get("/revisiones?month=2026-04").text)

    def test_an_unscorable_month_says_why(self):
        r = self.client.get("/revisiones?month=2026-01")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Este mes no tiene nota", r.text)

    def test_empty_ledger_is_a_page(self):
        self.conn.execute("DELETE FROM transactions")
        self.conn.commit()
        self.assertEqual(self.client.get("/revisiones").status_code, 200)


class Annual(WebBase):
    """/anual renders annual_matrix and links every figure to its movements."""

    def setUp(self):
        super().setUp()
        for month in ("2026-01", "2026-02", "2026-03", "2026-04"):
            self.add(f"{month}-01", 200000, "nomina", "ingresos-nomina")
            self.add(f"{month}-05", -30000, "mercadona", "supermercado")
        self.add("2026-02-10", -312900, "taller", "transporte-moto")
        self.add("2026-04-30", -100, "cierre", "supermercado")

    def test_renders_annual_matrix_and_links_each_cell(self):
        m = metrics_mod.annual_matrix(self.conn)
        r = self.client.get("/anual")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Anual · 2026-01 .. 2026-04", r.text)
        self.assertIn(eur(m["grand_total_cents"]), r.text)
        self.assertIn('href="/movimientos?month=2026-02&category=transporte-moto"', r.text)
        self.assertIn('href="/movimientos?month=2026-02&category=transporte"', r.text)
        # The parent is expandable without JS.
        self.assertIn('<details class="mx-group">', r.text)

    def test_a_spike_is_coloured_and_an_ordinary_month_is_not(self):
        r = self.client.get("/anual")
        cell = re.search(r'<a class="mx-cell fig ([^"]*)"\s+href="/movimientos\?month=2026-02&'
                         r'category=transporte"', r.text)
        self.assertEqual(cell.group(1).strip(), "hu3")
        cell = re.search(r'<a class="mx-cell fig ([^"]*)"\s+href="/movimientos\?month=2026-02&'
                         r'category=supermercado"', r.text)
        self.assertEqual(cell.group(1).strip(), "")

    def test_heat_bins_handle_negative_deviations(self):
        self.assertEqual(webapp.heat(-1.4), "hd3")
        self.assertEqual(webapp.heat(-0.3), "hd1")
        self.assertEqual(webapp.heat(0.1), "")
        self.assertEqual(webapp.heat(50), "hu3")

    def test_unknown_end_falls_back_to_the_latest_window(self):
        r = self.client.get("/anual?end=1999-13")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Anual · 2026-01 .. 2026-04", r.text)
        r = self.client.get("/anual?end=2026-02")
        self.assertIn("Anual · 2026-01 .. 2026-02", r.text)
        self.assertIn("Confianza baja", r.text)

    def test_nothing_on_the_page_is_loaded_over_the_network(self):
        r = self.client.get("/anual")
        for url in re.findall(r'(?:src|href)="([^"]+)"', r.text):
            self.assertFalse(url.startswith(("http://", "https://", "//")), url)


class AnnualEmpty(WebBase):
    def test_no_complete_month_is_a_page(self):
        self.add("2026-08-03", -3000, "mercadona", "supermercado")
        r = self.client.get("/anual")
        self.assertEqual(r.status_code, 200)
        self.assertIn("no hay año", r.text)


class Scenarios(WebBase):
    """/escenarios renders project_all and computes nothing (D22, D31)."""

    YAML = """
horizon_months: 6
min_balance_eur: 500
scenarios:
  - id: base
    name: Base
    events: []
  - id: nomina
    name: Con nómina
    events:
      - {label: Alquiler, monthly_eur: -400, from: 2026-06, replaces: vivienda}
      - {label: Nómina, monthly_eur: 1500, from: 2026-07}
      - {label: Matrícula, eur: -900, month: 2026-06}
"""

    def setUp(self):
        super().setUp()
        self._real_scen = scenarios_mod.SCENARIOS_PATH
        scenarios_mod.SCENARIOS_PATH = Path(self.tmp.name) / "scenarios.yaml"
        goals_mod.set_liquid(300000)   # a round start, independent of the fixture's balances
        for month in ("2026-01", "2026-02", "2026-03", "2026-04"):
            self.add(f"{month}-05", -80000, "mercadona", "supermercado")
            self.add(f"{month}-06", -20000, "alquiler", "vivienda-alquiler")
        self.add("2026-05-10", -100, "cierre", "supermercado")

    def tearDown(self):
        scenarios_mod.SCENARIOS_PATH = self._real_scen
        super().tearDown()

    def write(self, text):
        scenarios_mod.SCENARIOS_PATH.write_text(text, encoding="utf-8")

    def test_missing_file_says_how_to_create_one(self):
        r = self.client.get("/escenarios")
        self.assertEqual(r.status_code, 200)
        self.assertIn("No hay <code>", r.text)
        self.assertIn("min_balance_eur", r.text)

    def test_an_invalid_file_is_a_message_naming_the_event(self):
        self.write("scenarios:\n  - id: a\n    events: [{label: Paro, monthly_eur: 1, "
                   "from: 2027-02, until: 2026-12}]\n")
        r = self.client.get("/escenarios")
        self.assertEqual(r.status_code, 200)
        self.assertIn("no es válido", r.text)
        self.assertIn("&#39;Paro&#39;", r.text)

    def test_renders_exactly_what_project_all_returns(self):
        self.write(self.YAML)
        res = projection_mod.project_all(self.conn, scenarios_mod.load())
        r = self.client.get("/escenarios?s=nomina")
        self.assertEqual(r.status_code, 200)
        sel = next(s for s in res["scenarios"] if s["id"] == "nomina")
        for row in sel["rows"]:
            for k in ("opening_cents", "income_cents", "outflow_cents", "closing_cents"):
                self.assertIn(eur(row[k]), r.text)
        for s in res["scenarios"]:
            self.assertIn(eur(s["required_income_cents"]), r.text)
            self.assertIn(eur(s["final_cents"]), r.text)
            for month in (s["floor_month"], s["zero_month"]):
                if month:
                    self.assertIn(month, r.text)
        # The stub row and the replaced base are labelled, not silent.
        self.assertIn("2026-05*", r.text)
        self.assertIn("base sin vivienda", r.text)

    def test_unknown_scenario_falls_back_to_the_first(self):
        self.write(self.YAML)
        r = self.client.get("/escenarios?s=nope")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Mes a mes · Base", r.text)

    def test_nothing_on_the_page_is_loaded_over_the_network(self):
        self.write(self.YAML)
        r = self.client.get("/escenarios")
        for url in re.findall(r'(?:src|href)="([^"]+)"', r.text):
            self.assertFalse(url.startswith(("http://", "https://", "//")), url)

    def test_an_empty_ledger_with_a_file_is_a_page(self):
        self.write(self.YAML)
        self.conn.execute("DELETE FROM transactions")
        self.conn.commit()
        r = self.client.get("/escenarios")
        self.assertEqual(r.status_code, 200)
        self.assertIn("No hay desde dónde proyectar", r.text)


if __name__ == "__main__":
    unittest.main()
