{% macro fee_tax_to_micros(value_col) %}
{#- Epic 41 / Story 41.3 -- decimal display value -> EXACT integer micros.

    Cross-adapter, in the days_between.sql house style: DuckDB and BigQuery are the
    two dialects this repo targets, and they disagree on ONE thing here -- DuckDB
    needs an explicitly-parameterised DECIMAL(p,s) to stay in exact decimal
    arithmetic (an unqualified numeric expression silently promotes to DOUBLE and
    exactness is lost with no error), while BigQuery does not accept a
    parameterised DECIMAL inside CAST at all and offers bare NUMERIC, fixed at
    (38,9). That is the whole reason for the branch; it is not defensive.

    Contract (E41-NFR02 / epic C.6, amendment A.4):
      * `cost` reaches fact_daily_kpi as a display DECIMAL, never micros
        (dbt/seeds/money_metric_units.csv line 5: `cost,decimal`).
      * The ladder normalises PER SOURCE ROW and only then SUM()s exact BIGINTs.
        `ROUND(SUM(value) * 1e6)` would put the single rounding boundary AFTER the
        float drift instead of before it.
      * This mirrors server/core/money.py::to_canonical_micros(value, "decimal")
        = round(value * MICROS_PER_UNIT).

    Rounding policy: ROUND_HALF_UP (half away from zero), ONE rounding per value.
    Both DuckDB ROUND() and BigQuery ROUND() round half away from zero natively,
    and the DOUBLE -> DECIMAL(28,6) / FLOAT64 -> NUMERIC cast rounds the same way at
    6 decimal places, which IS the micro grain -- so the cast is the rounding.

    DOCUMENTED DIVERGENCE (do not "fix" it): money.py uses Python's round(), i.e.
    banker's rounding (half to even). At an exact half-micro the SQL and the Python
    adapter can differ by 1 micro. The SQL side is the authority for the ladder
    (arbitration B2). Fixture S2 lands on exactly such a half so the divergence is
    pinned by test_epic41_cascade_pinned_example.sql, not discovered later. -#}
{%- if target.type == 'bigquery' -%}
CAST(ROUND(CAST({{ value_col }} AS NUMERIC) * 1000000) AS BIGINT)
{%- else -%}
CAST(ROUND(CAST({{ value_col }} AS DECIMAL(28,6)) * 1000000) AS BIGINT)
{%- endif -%}
{% endmacro %}
