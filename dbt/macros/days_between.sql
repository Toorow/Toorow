{% macro days_between(start_col, end_col) %}
{#- Cross-adapter day difference (review 22.4 F-1): DuckDB supports DATE - DATE
    (integer days) but BigQuery requires DATE_DIFF. Returns end - start in days
    (0 when equal); callers add +1 for inclusive flight lengths. -#}
{%- if target.type == 'bigquery' -%}
DATE_DIFF({{ end_col }}, {{ start_col }}, DAY)
{%- else -%}
({{ end_col }} - {{ start_col }})
{%- endif -%}
{% endmacro %}
