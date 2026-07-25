-- semantic_avg_position: THE canonical aggregate for GSC average_position (AD-4).
-- Non-additive metric -- the impression-weighted mean is computed HERE, from
-- STAGING rows (page x country x device grain), never from mart aggregates:
-- weighting from pre-aggregated rows would average positions of unequal traffic.
-- review-6-2 fix: the earlier version self-joined fact_daily_kpi aggregates
-- (meaningless summed positions) and had ambiguous column references.
{{ config(materialized='view') }}

{% set dims = ["page", "country", "device"] %}
{% for dim in dims %}
SELECT
    project_id,
    date,
    'gsc'            AS connector,
    '{{ dim }}'      AS breakdown_dimension,
    {{ dim }}        AS breakdown_value,
    SUM(average_position * impressions) / NULLIF(SUM(impressions), 0) AS average_position,
    SUM(impressions) AS impressions_weight,
    SUM(impressions) AS semantic_weight,
    MAX(pull_id)     AS pull_id,
    MAX(loaded_at)   AS loaded_at
FROM {{ ref('stg_gsc_daily') }}
GROUP BY project_id, date, {{ dim }}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
