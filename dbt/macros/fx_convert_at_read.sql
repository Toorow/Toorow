{% macro fx_convert_at_read(amount_col, fx_rate_col='fx_rate') %}
{#- FX-at-read conversion macro (Story 39.10 / Amendment 2026-07-23 [[fx-locus-read-not-staging]]).
    Converts source-currency amount to project canonical currency at read time.
    Mirrors fx_helper.convert fixed tier rate selection. -#}
CAST({{ amount_col }} AS DOUBLE) * COALESCE({{ fx_rate_col }}, 1.0)
{% endmacro %}
