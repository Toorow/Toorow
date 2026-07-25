{% macro normalize_dimension(source_value, seed_ref, alias_col, canonical_col) %}
{#
  normalize_dimension: look up source_value in the aliases column of seed_ref
  and return the canonical column value (e.g. iso_code or canonical_value).

  When no match is found, returns NULL. Combined with a dbt not_null test on the
  normalized column, an unknown dimension value causes the dbt run to fail with a
  clear "not_null" violation — enforcing the vocabulary boundary (AC3, HG-2).

  Implementation: DuckDB scalar subquery using list_contains + string_split.
  string_split(aliases, '|') produces an array; list_contains checks membership.

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
    SELECT {{ canonical_col }}
    FROM {{ seed_ref }}
    WHERE list_contains(string_split({{ alias_col }}, '|'), {{ source_value }})
    LIMIT 1
)
{% endmacro %}
