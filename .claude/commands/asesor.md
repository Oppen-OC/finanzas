---
description: Monthly financial review - score the last complete month (or a period given in the prompt), explain it, and follow up on last month's commitments
argument-hint: "[YYYY-MM | last N months]"
---

Review a month of the user's spending as a critical, specific advisor, and write the
review to `reviews/YYYY-MM.md`. Read `docs/DESIGN.md` (D29) if you need the reasoning.

**The score is not yours.** `finanzas review` computes it, deterministically, from
the ledger and `goals.yaml`. Copy it exactly. Never compute, adjust, round
differently or "contextualize" it into another number, even when the month has a
good explanation: a planned purchase counts in full. Your job is the
words around the number. Same for every figure: quote what the command printed,
never re-add amounts yourself.

Never edit the database, `data/`, `rules.yaml`, `categories.yaml` or `goals.yaml`
from this command. If the review turns up a categorization problem, say so and
suggest `/finanzas`; if it suggests a limit, give the `finanzas goals --set` line
and let the user run it.

## 1. Pick the period

- No argument: the last **complete** month. `review` defaults to it. On the 22nd
  with exports up to the 20th, that is the previous calendar month, not this one.
- `YYYY-MM`: that month. If it is not complete, do not score it. Run
  `uv run python -m finanzas report --month YYYY-MM` and give a short pace review
  instead (limits, spend so far), saying plainly that it is a partial month.
- "last N months" / a range: `review --month <last> --months N`. Review each month
  briefly and the period as a whole. Do not average the scores into a new one;
  describe the trend.

## 2. Data first

```bash
uv run python -m finanzas check
uv run python -m finanzas review --month YYYY-MM --json
```

If `check` reports a chain break or unmasked digits, or `review` comes back with
`blocked` reasons, stop and report those. They are data problems, and a review on
top of them would be read as a verdict anyway.

If `low_confidence` is true, `opaque_share` of spend went to cash, misc or
uncategorized. **Open the review with that**, with the amount, before any other
judgment. Look at the big charges in those categories: a regular Bizum of the same
amount every month is probably a categorization gap (e.g. rent paid by Bizum
landing in `misc-bizum`), which is fixable with `/finanzas`, not a spending problem.

## 3. Read last month's review

`ls reviews/` and read the most recent review before this month, if one exists.
Its **Commitments** section is what you check first: for each commitment, find the
figure in this month's output that confirms or refutes it, and say which. A
commitment you cannot check against a number was badly written; say that too.

## 4. Ask before judging

Pick at most three of `big_charges`, the top `deviations` and `price_changes` that
you would otherwise have to guess about, and ask the user about them in one message
(AskUserQuestion when available). Example: "1,200 EUR on 06/08 in
compras-tecnologia: the new laptop, planned?" Do not ask about what the data already
explains. The answers go into the review as context; they do not change the score.

## 5. Write `reviews/YYYY-MM.md`

Keep the title line exactly in the form below: the Revisiones page reads the score
out of it and flags the review as out of date when it no longer matches
`finanzas review`. In English, direct and specific. No moralizing, no generic advice ("cook more at
home", "track your spending"). Every criticism names a figure. Structure:

```markdown
# Review YYYY-MM · score X.X/10

<one paragraph verdict: what kind of month it was and the single thing that decided it>

## Score
| component | points | weight | what it measured |
(one row per component in `components`, copied; say which component was left out
and why, e.g. no limits in force that month)

## What stood out
- big charges, top deviations by euros, price changes, each with the user's answer
  where they gave one
- opaque share, if above 20%

## Last month's commitments
- each one: kept / broken / not checkable, with the figure

## Recommendations
At most three. Each one: the action, the category, the figure it is based on, and
its effect in euros per month and in months of runway (runway component's
`liquid_cents` divided by the spend baseline). Nothing about a category that is
already under its limit and its median.

## Commitments for next month
One to three, each checkable against a number `review` prints (a category total,
a limit, the opaque share). Agree them with the user before writing them.
```

One fact to keep in mind while judging, from docs/DESIGN.md: income is not
behavior, which is why it is not in the score. Do not praise or blame a month for
its income.

## 6. Offer the commit

Show `git status --short`, propose `review: YYYY-MM, X.X/10`, and commit
`reviews/YYYY-MM.md` only if they say yes.
