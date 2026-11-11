---
description: Monthly round for the finanzas ledger - import the new bank exports, categorize them, extend rules.yaml for whatever is new, and write the monthly review when a month has closed
---

Run the whole monthly round for this ledger, in order, stopping where the
instructions say to stop. The user has just dropped one or more Santander `.xls`
and BBVA `.xlsx` exports into `data/inbox/`.

Read `.claude/skills/categorizar/SKILL.md` before step 4: it holds the rules for
writing rules. Read `docs/DESIGN.md` if you need the reasoning behind the design.

Never write a category onto a transaction with SQL, never edit the database, and
never touch `data/`. The only things you may change are `rules.yaml`, and
`manual.yaml` through `categorize --set`.

## 1. See what is waiting

```bash
ls -la data/inbox/
```

Name the files you found and which bank each one is. Files already imported are
skipped by hash later, so an inbox full of old exports is normal and fine.

## 2. Import, verify, categorize

```bash
uv run python -m finanzas update
```

This runs, in order: `import` (additive, skips files already seen by sha256, never
updates or deletes a stored transaction), `stats`, `check` and `categorize`.

Read its output and **stop and report, without categorizing anything, if**:

- `import` printed `ERROR` for a file. An unknown card or IBAN means a new account
  and the fix is `accounts.yaml`, not a workaround.
- `check` reports a chain break. That means a period is missing between two
  movements, or a row was counted twice. It prints the unexplained amount. The fix
  is a wider export from that bank, not a rule.
- `check` reports an internal transfer leg standing alone, or a mirrored pair not
  marked internal. That is money read as spend or income when it only moved between
  the user's own accounts. Propose the fix as a transfer rule in step 4 and flag it
  in the report; do not leave it silent.
- `check` reports an unmasked digit run. Constraint 6 is broken and a parser needs
  fixing before anything else.

If `update` ends with "every transaction is categorized", skip to step 6
(steps 7 and 8 still run).

## 3. Look at what is new

```bash
uv run python -m finanzas uncategorized
```

Grouped by normalized description, ordered by absolute amount. Work top down; the
first ten lines are almost always most of the money. Tell the user what you can
identify and what you cannot, before writing anything.

For a merchant whose name means nothing (`sumup *jrm`, `ccr 2 sl`), search
the web before guessing, and say what you found and how confident you are. A wrong
category is invisible once it is inside a total, so an honest `misc` beats a
confident guess.

## 4. Extend the rules, additively

New rules are **added**, existing ones are **left alone**:

- Append new entries to the section of `rules.yaml` where they belong. Keep the
  existing ids exactly as they are; ids are referenced by `category_src` in the
  ledger and by the diagnostics.
- Inserting a narrow rule *above* an existing broad one is additive and fine
  (a specific merchant above a chain pattern). Say why in the diff.
- Editing or deleting an existing rule is not routine. Do it only when that rule
  produces a wrong assignment, and then call it out separately in the report with
  what it used to match and what it matches now.
- Only categories that already exist in `categories.yaml`. If something genuinely
  has no home, propose the new category as its own decision and wait for an answer;
  do not smuggle it into a rule. The taxonomy is frozen on purpose: it is what makes
  month over month comparison mean anything.
- A one-off that will never repeat gets pinned instead of a rule:
  `uv run python -m finanzas categorize --set <id> <category> --note "what it was"`.

**Show the diff and ask for approval before applying it.**

## 5. Apply and verify

```bash
uv run python -m finanzas categorize      # run it twice: identical output both times
uv run python -m finanzas rules           # must be 0 dead, 0 shadowed
```

Then read back every category you touched:

```bash
uv run python -m finanzas list --category <id> --limit 50
```

and check each merchant actually belongs there. This is the step that catches the
mistakes tests cannot: an unanchored `on ?air` once matched the middle of "fnac
bonaire" and filed a bookshop as a gym.

If anything is still uncategorized, go back to step 3. Two rounds is normal.

## 6. Report

- files imported, transactions added, duplicates skipped, per account date range
- `check` result in one line
- coverage before and after, as a percentage
- rules added, one line each, in plain language: what merchant, what category
- anything pinned with `--set`, anything left in `misc`, anything you could not
  identify and want the user to name
- what the new month changed that is worth knowing: a new recurring charge, a
  merchant that stopped appearing, an unusually large movement

## 7. Write the review if a month has closed

```bash
uv run python -m finanzas review --json    # "month" = last complete month in the ledger
ls reviews/
```

If `reviews/<month>.md` does not exist yet, this import closed a month: write its
review now by following `.claude/commands/asesor.md` from step 2 for that month,
and fold its commit offer into step 8. Skip this step when the file already exists
(a mid-month import), or when `review` comes back `blocked` (report the reasons
instead). It runs only after steps 2 to 5, because the review reads the categories.

## 8. Offer the commit

`rules.yaml`, `manual.yaml` and any new `reviews/YYYY-MM.md` are the ledger's
source of truth alongside the exports, so they belong in git. The database never does. Show the user
`git status --short`, propose a commit message naming the month and the rules
added, and commit only if they say yes.
