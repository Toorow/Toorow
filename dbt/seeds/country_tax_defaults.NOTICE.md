# `country_tax_defaults.csv` — DEFAULTS REQUIRING OPERATOR CONFIRMATION, NOT TAX ADVICE

> **These are DEFAULTS REQUIRING OPERATOR CONFIRMATION, NOT TAX ADVICE.** Every row lands
> as `status='proposed'` and is **inert** until a human confirms it. Rates and effective
> dates change by jurisdiction and by contract; the platform ships a starting point so
> the operator reviews instead of typing JSON.

Story 41.2 (Epic 41, media cost composition). This seed is the **only** place a country
tax rate ships in this repository: a hard-coded rate map under `server/core/` is a defect
(E41-NFR04 / C.3), and `server/tests/conformance/test_no_geographic_hardcode.py` plus the
story-local AST guard in `server/tests/core/test_fee_tax_auto_population.py` both enforce
that.

## Why this file exists instead of a comment line inside the CSV

dbt-core loads seeds through agate's CSV reader, which has **no comment syntax**. A
leading `# ...` line would be parsed as the header row and the seed would break (or load
a single column named `#`). The disclaimer therefore lives in three places instead:

1. this sibling notice — dbt only picks up `.csv` files under `seed-paths`, so a `.md`
   beside the seed is inert and cannot break `dbt seed`;
2. the `load_country_tax_defaults` docstring in
   `server/core/fee_tax_country_defaults.py`;
3. the `source_note` column of **every** row, which the loader **enforces non-empty** —
   an honesty gate in code, not only in review.

A test asserts the CSV's first line is still the header and that this notice still carries
the "not tax advice" sentence, so a well-meaning future edit cannot reintroduce the
comment line or quietly drop the disclaimer.

## What the seed deliberately does NOT ship

- **No `US` row.** There is no federal VAT; US sales tax is state and local and often
  destination-based. A single national rate would be a fabrication. A US project gets a
  `no_default` count and the operator declares — that is the correct answer, not a gap.
- **No reduced or zero VAT rates, no reverse charge, no B2B exemption.** These are
  contract- and entity-specific. Standard rate only, and every `source_note` says so.
- **No digital services tax beyond GB / FR / ES / IT.** Other jurisdictions levy one at
  rates that vary by platform; shipping more would assert rates we have not verified.
- **No agency fee, platform fee or payment-gateway fee.** Those are per-contract and
  per-project; nothing about them is a country default.

## Adding a jurisdiction

Add **a row**, not code. The columns are
`iso_code,tax_category,form,rate,label,source_note,effective_from`:

- `iso_code` must exist in `dim_country.csv` (the legal ISO set);
- `tax_category` is `REGULATORY_TAX` or `SALES_TAX`;
- `form` is `PERCENTAGE` — a country default that is not a percentage is refused rather
  than guessed;
- `rate` is an exact decimal fraction in `[0, 1)` at six decimals (`0.030000` = 3%);
- `source_note` must be non-empty and must say what the number is and what it is not;
- `effective_from` is an ISO date.

The loader is fail-closed on every one of those rules, and on a duplicate
`(iso_code, tax_category, effective_from)`.
