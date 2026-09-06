{% macro normalize_dimension(source_value, seed_ref, alias_col, canonical_col) %}
{#
  normalize_dimension: look up source_value in the aliases column of seed_ref
  and return the canonical column value (e.g. iso_code or canonical_value).

  When no match is found, returns NULL. Combined with a dbt not_null test on the
  normalized column, an unknown dimension value causes the dbt run to fail with a
  clear "not_null" violation — enforcing the vocabulary boundary (AC3, HG-2).

  IMPLEMENTATION: A SENTINEL-DELIMITED SUBSTRING SEARCH, AND NOT AN ARRAY.
  This macro used to spell membership `list_contains(string_split(aliases, '|'),
  value)`. Both of those functions are DuckDB's alone: BigQuery answers
  *Function not found: list_contains* and the six connector stagings that call
  this macro could not run in production (measured 2026-08-31, the first real
  per-project run — `stg_youtube_breakdown`, `stg_gsc_daily`, `stg_cm360_daily`,
  `stg_dv360_daily`, `stg_taboola_daily`, `stg_ga4_standard_daily`).

  `STRPOS` and `||` exist on BOTH engines with the same semantics, so the
  membership test needs no dialect branch at all — which is why this is a
  rewrite and not a third macro in `engine_types.sql`. Wrapping BOTH sides in
  the '|' separator is what keeps it an EXACT membership test rather than a
  substring match: 'FR' matches 'FR|FRA' through '|FR|', and never matches
  'AFR' — the naive `STRPOS(aliases, value)` would have said yes to that.
  It is also wildcard-safe, which a `LIKE` spelling would not have been.

  NULL travels the same way it did before: a NULL alias list or a NULL
  source_value concatenates to NULL, STRPOS returns NULL, no row is matched.

  AND IT IS AN AGGREGATE, NOT A `LIMIT 1`. That single word was the SECOND
  production refusal of this macro, and it survived the first repair because the
  view COMPILED: BigQuery answers *Correlated subqueries that reference other
  tables are not supported unless they can be de-correlated* only when it has to
  PLAN the scalar subquery, which is why `stg_youtube_breakdown` built and its
  `not_null` tests died (revision mcp-server-00226, project
  proj_01KZGCRSV2XACWRP3RSVNWWGBK).

  The rule was measured directly against BigQuery on 2026-08-31, six dry runs at
  0 bytes, one construct each. What decides it is the LIMIT and not the
  predicate:

    scalar + LIMIT 1 + STRPOS(...) > 0     REFUSED
    scalar + LIMIT 1 + equality            REFUSED
    scalar + ARRAY_AGG(... LIMIT 1)        REFUSED
    scalar + MIN(...) + STRPOS(...) > 0    ACCEPTED   <- this macro
    scalar + MIN(...) + equality           ACCEPTED
    LEFT JOIN ... ON STRPOS(...) > 0       ACCEPTED

  So the aggregate form needs no JOIN at every call site and no dialect branch;
  DuckDB plans it identically. It is also STRICTLY MORE DEFINED than what it
  replaces: `LIMIT 1` over several matching alias rows returned whichever row the
  engine reached first, so two engines -- or one engine after a seed reorder --
  could canonicalise the same value differently. `MIN` names one of them and
  always the same one. ON THE VOCABULARY THIS REPO SHIPS THE TWO ANSWER THE SAME
  THING FOR EVERY POSSIBLE INPUT, and that is measured rather than assumed: over
  the 1157 distinct alias tokens of `dim_country` and the 12 of `dim_device`, the
  maximum number of seed rows a token matches is 1 (2026-08-31). With at most one
  candidate row there is nothing for `LIMIT 1` to choose BETWEEN, so it and `MIN`
  return the same value -- and both return NULL on a value that matches none.
  Confirmed on the fixture warehouse, two full harness builds with only this line
  changed: identical row counts and an identical md5 over the multiset of
  (source spelling -> canonical value) pairs, per relation -- stg_cm360_daily 3,
  stg_dv360_daily 3, stg_taboola_daily 3, stg_gsc_daily 900,
  stg_ga4_standard_daily 1350 (country and device_category both).
  (A hash of the WHOLE row is not the instrument here and would say nothing: the
  stagings carry `pull_id` and `loaded_at`, which are fresh on every build.)

  NULL still travels: `MIN` over zero matching rows is NULL, exactly what an
  empty `LIMIT 1` returned, so the fail-closed vocabulary boundary is unchanged.

  Args:
    source_value: SQL expression evaluating to the raw dimension value (e.g. 'country').
    seed_ref:     dbt ref() to the seed table (e.g. ref('dim_country')).
    alias_col:    column in seed containing pipe-delimited aliases (e.g. 'aliases').
    canonical_col: column in seed to return (e.g. 'iso_code' or 'canonical_value').

  Usage:
    {{ normalize_dimension('country', ref('dim_country'), 'aliases', 'iso_code') }}
    {{ normalize_dimension('device_category', ref('dim_device'), 'aliases', 'canonical_value') }}

  HG-1 (AD-2): this macro is source-agnostic — no module names appear here.
  HG-2: unknown value => NULL => not_null test failure => dbt run failure.
#}
(
    SELECT MIN({{ canonical_col }})
    FROM {{ seed_ref }}
    WHERE STRPOS('|' || {{ alias_col }} || '|', '|' || {{ source_value }} || '|') > 0
)
{% endmacro %}
