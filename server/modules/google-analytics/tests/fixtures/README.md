# Golden pull fixtures (conformance layer 4)

- `golden_pull.json` — raw rows as landed by a pull.
- `expected_facts.json` — canonical fact rows expected from `connector.transform()`.

`transform()` reads `manifest.json` mappings AT CALL TIME (review-1-8 F-03):
whenever `canonical_metric_mapping` / `canonical_dimension_mapping` change,
REGENERATE `expected_facts.json` accordingly — a stale file makes the golden
layer assert yesterday's contract. `transform()` is a proxy for the dbt
staging logic (which additionally applies the QUALIFY supersede dedup, AD-7);
keep fixtures single-pull so both stay equivalent.
