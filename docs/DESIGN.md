# Design notes

Every non-obvious decision in this codebase, and why it was made. Code comments
cite these by number (`D2`, `C6`): this is where the reference resolves. Most of
the surprising choices are surprising because of something a real bank export did.

## Constraints

Settled before the first line of code. If a feature seems to need breaking one,
the feature is wrong, not the constraint.

| # | Constraint | Consequence |
|---|---|---|
| C1 | Runs entirely on the local machine | No hosted services, no external accounts, no CDN |
| C2 | The app never calls an LLM API | LLM work happens in Claude Code and is persisted as code or data |
| C3 | Single user, personal scale | No auth, no multi tenancy, no queues; bound to loopback |
| C4 | Simplest structure that works | Flat modules, no ORM, no service layers, no DI, no single-impl ABCs |
| C5 | Ingestion is additive | Never update, overwrite or delete a stored transaction |
| C6 | No long digit runs persisted | Card numbers and IBANs are masked in the parser, before the DB |
| C7 | Euros only | A non-EUR row raises; nothing is converted silently |
| C8 | Money as integer cents | No floats anywhere in the ledger |

## Build vs adopt

Actual Budget and Firefly III were the serious candidates. Their value for this use
case would have been the AI categorization add-ons built around them, and every one
of those is a daemon calling an LLM API on a schedule, which C2 rules out. Without
that layer, what remains is an envelope-budgeting UI nobody asked for, around a data
store that is awkward to query directly. Adapting either would have meant keeping
the ingestion and the schema and discarding the rest, and those are the parts worth
writing. Excel as the tool was rejected too: it cannot do idempotent merges of
overlapping exports, which is the hardest requirement here. Excel stays an input
format.

## What the bank files actually look like

Verified against real exports, not documentation.

- **Santander**: legacy `.xls`, sheet `Movimientos`, a preamble holding the IBAN and
  balance, then `FECHA OPERACIÓN | FECHA VALOR | CONCEPTO | IMPORTE EUR | SALDO`,
  newest first. Some concept lines contain a full card number in clear text.
- **BBVA**: `.xlsx`, sheet `Informe BBVA`, no IBAN anywhere, so account identity
  comes from the card number that starts every `Observaciones` cell. For Bizum,
  `Concepto` just says "Bizum"; the content lives in `Movimiento`.
- **Neither bank provides transaction IDs.** This is the central technical problem
  and it shapes the whole ingestion design.
- **Both provide a running balance, and the chain is exact**: in file order,
  `balance[i] - amount[i] == balance[i+1]` for every pair. File order is therefore
  true transaction order even where dates jump around, and the balance becomes the
  basis for identity and integrity checks.

## Ingestion and identity

**D1. Three storage layers: `imports` → `raw_rows` → `transactions`.** Every file is
recorded by sha256, so re-importing it is a no-op. Every row is stored verbatim as
JSON. The ledger is derived from those, so a bad parsing or dedupe decision can be
replayed later without asking the bank for the file again.

**D2. The running balance is part of the dedupe key.** The obvious key, (account,
date, amount, description), is wrong: a real export contained four identical Bizum
rows on the same day, and another fourteen identical 1.00 EUR contactless payments.
Collapsing them would quietly lose real money. The key is `(account, value date,
amount, balance, description_norm, occurrence)`, where `occurrence` numbers repeats
of the same tuple inside one file (a balance returning to the same value on the same
day: +5, −5, +5).

**D3. The balance chain outranks textual similarity.** A first implementation flagged
legitimate rows as near-duplicates, because identical transactions look like
duplicates by any textual measure. Two rows with different balances are different
movements of money, however identical the text.

**D4. No quarantine.** With description in the key, a reworded row is a new row. The
accepted risk, a bank rewording an already exported row and the re-export being
stored twice, is caught by D11.

**D5. `date` means the value date, not the booking date.** For a card payment, the
value date is when the money was spent; booking is up to two days later. Monthly
spend follows the former; the booking date is kept in `date_booked`.

**D6. Long digit runs never reach the database.** Any run of 12+ digits, or a card
number written in 4-digit groups, is cut to its last 4 inside the parser. `{12,}`
and not `{12,19}`: an upper bound would leak the tail of a 22-digit IBAN body. `check`
re-scans every persisted text column for long runs.

**D9. Account identity lives in `accounts.yaml`**, as the last 4 digits each account's
exports reveal. An unknown identifier stops the import instead of opening a new
account, because a replaced card would otherwise split one balance chain in two.

**D10. `data/inbox/` is a dump folder.** Nothing is moved, renamed or deleted; files
already imported are skipped by hash, so exports can stay there forever.

**D11. The ledger-wide balance chain is the backstop for dedupe.** `check` orders each
account by following the balance and reports every break with the unexplained amount.
A missing period and a double-counted row both break the chain.

**D24. The upload endpoint runs the whole round, and never overwrites a source file.**
`finanzas update` and the web upload are two renderings of one function in
`update.py`, so the page cannot disagree with the verified CLI. An uploaded filename is
client supplied, so only its basename is used, and a collision is saved beside the
existing file rather than over it: banks name exports generically, and overwriting
would destroy an export the raw layer exists to replay.

## Categorization

**D7. The LLM writes rules, not labels.** Categorization is deterministic regex rules
in a version-controlled `rules.yaml`, applied by the app. Claude only ever sees the
transactions the rules failed to match, and its output is new rule entries, reviewed
as a diff. Consequence: the same input always produces the same output, last month's
numbers never move on their own, every classification traces to a line in a file,
and the unmatched count falls toward zero over time. This is what makes C2 workable
rather than limiting.

**D12. Bizum is a payment method, not a category.** A `bizum` tree would make every
category total depend on how a bill happened to be paid: the same dinner in `salidas`
by card and in `bizum-comida` when a friend covers it. The data settled it: a few
large Bizums carry most of the money and have readable concepts, so they get rules by
purpose, and the unreadable tail goes to one bounded `misc-bizum` bucket.

**D13. A refund lands in the category it refunds, with a positive amount.** A returned
shirt, a friend's share of dinner coming back by Bizum. A category total is the net
cost and can be negative in a month, so metrics sum per category and never filter by
sign.

**D14. Three category types: `expense`, `income`, `internal`.** Transfers between your
own accounts and out to your own investment account are `internal`, excluded from
spend and income alike. Because one leg is recognized by a free-text concept line,
`check` pairs the two legs by mirrored amount within 3 days and reports any leg
standing alone, plus mirrored pairs that are not marked internal. It caught a real
miss on its first run.

**D15. Hand-set categories live in `manual.yaml`, not in the database.** The database
is disposable by design, so an override stored only there dies with the next rebuild.
Entries are keyed by a hash of the natural key (row ids are reassigned on import).
The files, inbox plus `rules.yaml` plus `manual.yaml`, fully determine every category.

**D18. `fixed`, `discretionary` and `opaque` are flags in `categories.yaml`, not
constants in `metrics.py`.** They inherit from parent to child, can be overridden per
child, and fail at load time on a typo like every other category reference. `fixed`
is how a gym that changes name every few months still counts as a monthly commitment.

**D20. A tax refund is income, not a negative expense.** It looks like a D13 refund
and is not one: the withholding it refunds was never in the ledger, because salary
arrives net. General rule: money arriving nets against an expense category only when
the matching outflow is also in the ledger.

**D23. A filter on a parent category includes its children**, and "uncategorized" is a
reserved sentinel rather than an empty string.

## Metrics

**D8 / C8. Money as integer cents everywhere.** Floats are converted once, at the
parser, through `Decimal(repr(x))`, and a sub-cent amount raises.

**D16. Metrics run over complete months only, and coverage is the intersection across
accounts.** A partial month averaged into a baseline reads as a 60% fall in spending
and poisons every median that uses it. A month where one bank's export is missing is
not a month you can report on. Anything resting on fewer than three complete months
is flagged `low_confidence`.

**D17. Recurring detection clusters by amount before it measures cadence.** Grouping by
merchant and looking at the gaps fails on the most regular subscription there is: one
overage charge on a different day turns monthly gaps of 30–31 days into a median of
22. So charges within 10% of the group median are selected first and the cadence is
measured over that cluster alone, with "more than half the gaps fall in the window"
as the test, which survives one skipped month.

**D19. The burn rate window is 12 months; per-category baselines stay at 3.** A
category median should react to recent change. The burn rate feeds the runway, a
decision number, and has to survive one unusual purchase: on real data a 3-month
median nearly doubled the burn because one window happened to contain two outliers.

**D22. The web UI renders `metrics.py` and computes nothing.** The numbers were verified
against the CLI; a route that does its own arithmetic is a number nobody verified.

**D27. Month-to-date pace is measured as of the end of the ledger, not the wall
clock**, and `fixed` categories are not projected. If the last export covers the 10th
and today is the 20th, those ten days are missing data, not ten days of restraint.
And rent landing on the 2nd means the month's cost is known, not that the month is
heading for fifteen times it.

## Configuration as files

**D25. Limits and settings live in `goals.yaml`, not in a table**, for D15's reason:
the database is derived and disposable, and deleting it and re-importing is a
verification step that actually gets run.

**D26. A limit carries the month it starts from.** Raising a limit in November must not
retroactively make October look disciplined: entries are superseded, never edited in
place.

**D28. A backup is a fast restore, not the durability story.** `backup` uses SQLite's
backup API because a file copy of a live database can be torn mid-page. But what
keeps the history safe is the inbox plus the YAML files, and the command says so.

## Front end

**D21. Chart.js and htmx are vendored, not loaded from a CDN.** A CDN script tag is a
hosted service in the request path (C1): the dashboard would break offline and
announce every page view to a third party. Both are pinned, licensed, and marked
`-text` in `.gitattributes` so they stay byte-identical to upstream. A test asserts no
rendered `src` or `href` starts with a scheme.

**D30. The annual view is a category × month matrix, coloured per row.** On an absolute
scale housing and groceries wash out everything else, so each cell is coloured by its
deviation from its own row's median (with a 50 EUR floor). Zero months count toward
the median, so an annual charge reads as the spike it is. The footer is the month's
spend from the same pass the dashboard uses, so the rows adding up to it is a checked
property, not a tautology.

The UI is in Spanish and the CLI in English, deliberately: the category names come
from `categories.yaml` and are Spanish, and a page that mixes the two reads worse
than two surfaces that each pick one.

## The LLM half

**D29. The monthly review's score is computed by the app; Claude writes the words.**
`finanzas review` returns a score out of 10 and the talking points deterministically:
spend against the trailing 12-month median (40), limits in force that month (25),
discretionary spend against its median (15), runway at month end on an absolute 2–6
month scale (20). A component that does not apply is dropped and the rest reweighted.
Income is deliberately absent: it is often an event (a bonus, a tax refund), not
behavior. `/asesor` copies the score, never adjusts it, writes the commentary to
`reviews/YYYY-MM.md`, and checks last month's commitments against this month's
numbers. That loop is the feature. A review whose score no longer matches the ledger
(a late export, a new rule) is flagged as out of date in the UI.

**D31. Scenarios project the balance month by month from stated events.** The runway
is one number and cannot represent a change of rent or income. `scenarios.yaml` holds
named scenarios of signed events, recurring or one-off; `projection.py` is arithmetic
over them, not a forecast. `replaces` removes a category from every month before the
median is taken, because medians do not subtract. The projection starts at the end of
the ledger (D27), with the rest of that month prorated and no events. Break-even is
computed over open-ended events only.
