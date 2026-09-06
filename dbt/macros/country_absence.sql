{% macro country_absent_sentinel() %}'__country_absent__'{% endmacro %}

{% macro country_bucket(canonical, source, retain_source_value=false) %}
{#
  country_bucket: the ONE declared bucket for a row the source gave no country for
  (story 58.5, arbitrage 1).

  WHY IT EXISTS. The same fact -- a row with no country signal -- had two opposite
  behaviours in this mart, and both were latent (measured 2026-08-07: 0 rows on the
  ga4 side, 0 on the cm360 side, so neither had ever fired):

    * fact_daily_kpi composes `country || '>' || device_category`. A concatenation
      with NULL is NULL in SQL, so `breakdown_value` became NULL and the not_null
      test of schema.yml failed -- the NIGHTLY BUILD breaks. Loud, and repairable.
    * int_country_daily_kpi filtered `COALESCE(country, country_source) IS NOT NULL`,
      so the row was DROPPED. Silent, and the total by country stopped equalling the
      total of the day with nobody learning of it. That one is worse.

  So the absence gets a NAME, at the mart, on both paths. The sentinel is NOT a
  country code -- it can never be one: `country_vocabulary` refuses anything that is
  not `[A-Z]{2}`, and `dim_country.csv` holds 250 codes, none of them this. Readers
  qualify it by its own kind (`geography_bucket_kind`), never as a place: the Python
  side declares the same literal in `core.geographic_semantics`
  (COUNTRY_ABSENT_BUCKET_ID) and `tests/core/test_geographic_semantics.py` asserts
  the two spellings are one.

  IT FIRES ONLY WHEN THE SOURCE RENDERED NOTHING, and that is the whole precision of
  it. Three states, three answers:

    * the value resolved            -> the canonical code;
    * the source rendered a value
      that does not resolve         -> `retain_source_value=false` keeps NULL, so the
                                       vocabulary boundary of `normalize_dimension`
                                       still breaks the build ("unknown value => NULL
                                       => not_null test failure"), which is a
                                       deliberate fail-closed and not this macro's
                                       business. `retain_source_value=true` keeps the
                                       raw spelling observable, which is what
                                       int_country_daily_kpi already did and still
                                       does for governed DQ;
    * the source rendered nothing   -> the sentinel, and the row is KEPT.

  "Empty" and "broken" stay two different sentences. Folding the unresolved case into
  the sentinel would silence the vocabulary guard, which is the opposite repair.

  Args:
    canonical: the normalized country expression (NULL when it did not resolve).
    source:    the raw country expression as the provider rendered it (NULL when the
               provider rendered none).
    retain_source_value: keep the raw spelling when it did not resolve.
#}
{%- if retain_source_value -%}
COALESCE({{ canonical }}, {{ source }}, {{ country_absent_sentinel() }})
{%- else -%}
COALESCE({{ canonical }}, CASE WHEN {{ source }} IS NULL THEN {{ country_absent_sentinel() }} END)
{%- endif -%}
{% endmacro %}
