---
name: categorizar
description: Propose new rules.yaml entries for uncategorized transactions in the finanzas ledger. Use when coverage drops after importing new bank exports, or when asked to categorize, classify or clean up uncategorized movements.
---

# Categorizar movimientos

You write **rules**, never labels. Never set a category on a transaction directly,
never write SQL that touches `category`, never edit the database. Your output is a
diff against `rules.yaml` (or, for a genuine one-off, a `categorize --set` command).
This is what keeps the ledger reproducible: the same exports plus the same files
always produce the same categories, and last month's numbers never move on their own.

## The loop

1. `uv run python -m finanzas uncategorized`
   Groups every uncategorized transaction by normalized description, largest
   absolute total first. Work top down: the money is in the first ten lines.
   Add `--include-misc` for a periodic pass over what was parked in `misc`.
2. Read `categories.yaml`. **Every category you propose must already exist there.**
   If something genuinely has no home, say so and propose the category as a separate
   decision. Do not invent one inside a rule; the loader will reject it anyway.
3. Propose the new rules as a diff, with a one-line reason for each. Say which
   section of `rules.yaml` each belongs in and, if order matters, why.
4. `uv run python -m finanzas categorize --dry-run` to see what would change,
   then `categorize` to apply.
5. `uv run python -m finanzas rules` must report 0 dead and 0 shadowed.

## Writing a good rule

- `match` is a regex searched against `description_norm`: lowercase, accents
  stripped, single-spaced. Optional narrowing: `account`, `sign` (`+`/`-`),
  `amount_min`, `amount_max` (euros, absolute value).
- **Anchor merchant names.** `on ?air` once matched the middle of "fnac bonaire"
  and filed a bookshop as a gym. Use `\bname\b` unless you have a reason not to.
- **Prefer the specific over the broad.** One regex per chain is right
  (`mercadona` covers ten branch spellings); one regex per shopping trip is not.
- Santander truncates the merchant to about twelve characters and may prefix a till
  number: Consum arrives as `123456789 consu`, McDonald's as `mc donald's ald`.
  Match the stable fragment.
- **Order is the only precedence.** A narrow rule goes above the broad rule that
  would otherwise swallow it (the promotional Bizum above the supermarket rule).
  Everything below `bizum-sin-clasificar` never sees a Bizum.
- **Do not constrain the sign** unless the rule really is direction-specific. A
  refund, or a friend's share coming back, belongs in the same category with a
  positive amount so the total is the net cost.
- Comment the non-obvious ones: what the merchant actually is, or what the rule
  protects against. `sumup *jrm` and `ccr 2 sl` mean nothing to a reader
  six months from now.

## When a rule is the wrong tool

A single unrepeatable movement does not deserve a rule. Pin it instead:

```
uv run python -m finanzas categorize --set <id> <category> --note "what it was"
```

That writes `manual.yaml`, keyed by the transaction's natural key so it survives a
database rebuild. Rules never overwrite a pinned transaction.

If the description says nothing at all and nobody remembers it (a Bizum reading
"xd"), it belongs in `misc-bizum`, not in a guess. Guessing is worse than a
visible gap: a wrong category is invisible once it is in a total.

## Definition of done for a pass

- coverage reported by `categorize` above 85% (80% for historical backfill)
- `rules` reports 0 dead and 0 shadowed
- `categorize` run twice prints identical counts
- you can state, for each rule you added, what the merchant is
