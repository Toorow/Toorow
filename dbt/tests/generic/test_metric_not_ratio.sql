-- Generic dbt test: mart metric must NOT be a ratio (AD-4), source-agnostic.
--
-- Story 3.6 (FR2): the previous accepted_values gate listed connector-specific
-- metric names and therefore had to be edited for every new module. This test
-- is source-agnostic: it fails only if a metric name is declared non-additive.
--
-- 2026-08-20: the name list was HARDCODED here and had drifted from the
-- canonical vocabulary -- it missed average_position, unique_reach,
-- average_frequency and viewability_rate, all declared additive=false in
-- dbt/seeds/dim_metric.csv. The names are now DERIVED from that seed (the same
-- read scripts/check_non_additive_guard.py does), so an eighth non-additive
-- metric enters the gate without anyone thinking of it. The residual list
-- below keeps ratio names the vocabulary does not carry yet (cpc, cpm, ...) --
-- a name moving into dim_metric.csv leaves it without an edit here.
--
-- 2026-08-24 (AI-314): THIS TEST COULD NOT RUN IN PRODUCTION AT ALL. `VALUES` as
-- a standalone table constructor and `cast(x AS varchar)` are DuckDB/Postgres
-- spellings; BigQuery answers *Syntax error: Expected keyword JOIN but got ","*
-- and has no VARCHAR. So the one gate that keeps a ratio out of an additive fact
-- was green locally and refused on the engine the nightly runs on -- where a
-- refused test is an exit code, and an exit code is a Project with no marts.
-- Measured by a BigQuery dry run of the compiled test (0 bytes, free). The
-- residual names are now a UNION of one-row SELECTs, which both engines accept,
-- and the seed's boolean is compared with dbt's own portable string cast.
{% test metric_not_ratio(model, column_name) %}
{%- set residual_names = [
    'cvr', 'cpc', 'cpm', 'roi',
    'sessions_per_user', 'bounce_rate', 'engagement_rate', 'frequency',
] -%}
SELECT *
FROM {{ model }}
WHERE lower({{ column_name }}) IN (
    SELECT lower(name)
    FROM {{ ref('dim_metric') }}
    WHERE lower({{ dbt.cast_bool_to_text('additive') }}) = 'false'
    {%- for residual in residual_names %}
    UNION ALL
    SELECT '{{ residual }}'
    {%- endfor %}
)
{% endtest %}
