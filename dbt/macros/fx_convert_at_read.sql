{% macro toorow_exact_money_type() %}
{#- THE EXACT DECIMAL THIS PRODUCT DOES MONEY IN -- 38 digits, 9 fractional --
    spelled the way each engine will accept it.

    A literal `CAST(x AS DECIMAL(38, 9))` is what every money macro emitted until
    2026-08-25, and BigQuery refuses it outright: *Parameterized types are not
    allowed in CAST expressions*. Measured with a dry run over the compiled
    `stg_meta_ads_daily` (0 bytes, no table read) while proving the Story 67.13
    FX cutover -- so `fx_convert_at_read` and `money_evidence_present`, and
    therefore every monetary column of `fact_daily_kpi`, could not compile on the
    engine production runs while building green locally. The same class as the
    `::`/`VARCHAR` defect `money_evidence_absent()` documents, found the same way.

    BigQuery's bare `NUMERIC` IS DECIMAL(38, 9) -- exactly this type, not an
    approximation of it, so nothing is widened or narrowed by the branch. DuckDB's
    bare `NUMERIC` is DECIMAL(18, 3) and would silently truncate, which is why
    this cannot be one word for both. `dbt.type_numeric()` is likewise not it:
    it resolves to `numeric(28, 6)` and would drop three digits of a rate.

    A defensive branch is what `fee_tax_condition_matcher` forbids; this is the
    opposite case, and the counter-example that macro itself names: a
    parameterised decimal width genuinely needs one. -#}
{#- The branch itself now lives ONCE, in `engine_types.sql`, beside the same
    branch for `DOUBLE` and `VARCHAR` (execution-substrate "Incomplete if" 23).
    This macro keeps its own name because 38,9 is a MONEY decision -- Story
    48.3's exactness rule -- and not a width any caller may choose. -#}
{{- toorow_decimal_type(38, 9) -}}
{% endmacro %}


{% macro fx_convert_at_read(amount_col, fx_rate_col='fx_rate') %}
{#- FX-at-read conversion (Story 39.10, REPAIRED by Story 48.3).

    What this macro used to be:

        CAST({{ amount_col }} AS DOUBLE) * COALESCE({{ fx_rate_col }}, 1.0)

    Two defects, both load-bearing:

    1. `COALESCE(fx_rate, 1.0)`. When no rate was resolved the row was converted
       AT PARITY and summed into the total. A USD figure landed in a EUR total as
       though a dollar were a euro, and no column anywhere recorded that it had
       happened. This is the Currency & FX criterion "a cross-currency total
       succeeds without explicit FX evidence", written in SQL.
    2. `CAST(... AS DOUBLE)`. A binary float cannot hold a published rate or a
       money amount exactly, so the mart total and the application total for the
       same rows disagreed in the low digits.

    What it is now: NULL propagates. An unconvertible row yields NULL, which is
    excluded by SUM rather than silently added at parity, and the companion
    `fx_gap_code` column says why. DECIMAL(38, 9) is exact: 9 fractional digits
    hold canonical micros with room for the rate's own precision, and the product
    of two DECIMALs stays a DECIMAL.

    The row is NOT dropped -- `fact_daily_kpi` keeps it with a null converted
    value and a gap code, because deleting it would erase the evidence that
    something needs repairing (the "traces of missing work are preserved" rule).
-#}
CASE
    WHEN {{ fx_rate_col }} IS NULL THEN NULL
    ELSE CAST({{ amount_col }} AS {{ toorow_exact_money_type() }}) * CAST({{ fx_rate_col }} AS {{ toorow_exact_money_type() }})
END
{% endmacro %}


{% macro fx_gap_code(fx_rate_col='fx_rate', currency_col=none) %}
{#- The typed reason a row has no reporting value. Mirrors the Python gap codes in
    server/core/money_derivation.py so a warehouse reader and an application reader
    name the same thing. NULL means the row converted. -#}
CASE
{%- if currency_col %}
    WHEN {{ currency_col }} IS NULL THEN 'native_currency_missing'
{%- endif %}
    WHEN {{ fx_rate_col }} IS NULL THEN 'fx_rate_unavailable'
    ELSE NULL
END
{% endmacro %}
