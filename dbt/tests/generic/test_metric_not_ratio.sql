-- Generic dbt test: mart metric must NOT be a ratio (AD-4), source-agnostic.
--
-- Story 3.6 (FR2): the previous accepted_values gate listed connector-specific
-- metric names and therefore had to be edited for every new module. This test
-- is source-agnostic: it fails only if a metric name matches a known ratio /
-- non-additive pattern. Adding module N+1 needs NO edit here -- any additive
-- metric name (sessions, cost, impressions, spend, ...) passes automatically.
{% test metric_not_ratio(model, column_name) %}
SELECT *
FROM {{ model }}
WHERE lower({{ column_name }}) IN (
    'ctr', 'cvr', 'cpc', 'cpm', 'cpa', 'roas', 'roi',
    'sessions_per_user', 'bounce_rate', 'engagement_rate', 'frequency'
)
{% endtest %}
