-- semantic_avg_position_composite: impression-weighted average_position at the
-- country>device COMPOSITE grain for GSC (AI-52, Epic 8, AD-4 non-additive follow-up).
--
-- WHY THIS VIEW EXISTS
-- Story 8.11 v1 deliberately EXCLUDED non-additive metrics from composite emission
-- (test_composite_additive_only.sql guards it): summing average_position across a
-- country x device grid is meaningless, and a naive average of per-cell positions is
-- wrong because cells carry unequal traffic. AI-51 added the additive country>device
-- composite (clicks, impressions) to fact_daily_kpi for GSC; this view supplies the
-- CORRECT non-additive companion so a widget can show a country>device breakdown of
-- BOTH additive metrics and average_position from a single, honest source.
--
-- DESIGN DECISION -- TRUE COMPOSITE, NOT A FALLBACK
-- The source rows SUPPORT true country>device weighting. stg_gsc_daily lands page,
-- country AND device on the SAME row (raw grain project_id x date x page x country x
-- device), so country and device co-occur on every row and impressions is a real
-- per-(country,device) weight. We therefore weight at the exact composite grain:
--   SUM(average_position * impressions) / NULLIF(SUM(impressions), 0)
--   GROUP BY project_id, date, country, device
-- computed DIRECTLY from staging rows (NEVER from mart aggregates -- weighting from
-- pre-aggregated country or device rows would average positions of unequal traffic;
-- this is the same review-6-2 correctness rule that semantic_avg_position follows).
-- Because we roll page up within each (country, device) cell, this is the honest,
-- data-backed composite -- no fabricated numbers, no fallback needed.
--
-- ZERO-DIVISION SAFETY: NULLIF(SUM(impressions), 0) yields NULL (not a divide error)
-- for any (country, device) cell whose impressions sum to zero. NULL is the honest
-- answer: no impressions => no weight => no defined weighted position.
--
-- ENCODING: mirrors the fact composite exactly so a caller can join/align on grain --
-- breakdown_dimension = 'country>device', breakdown_value = '<country>>​<device>'
-- (e.g. 'fra>mobile'), parent>child order. This is a VIEW: non-additive metrics are
-- NEVER stored pre-aggregated (HG-3).
{{ config(materialized='view') }}

SELECT
    project_id,
    date,
    'gsc'                              AS connector,
    'country>device'                   AS breakdown_dimension,
    country || '>' || device           AS breakdown_value,
    country                            AS country,
    device                             AS device,
    SUM(average_position * impressions) / NULLIF(SUM(impressions), 0) AS average_position,
    SUM(impressions)                   AS impressions_weight,
    MAX(pull_id)                       AS pull_id,
    MAX(loaded_at)                     AS loaded_at
FROM {{ ref('stg_gsc_daily') }}
GROUP BY project_id, date, country, device

UNION ALL

-- Story 10.5: impression-weighted average_position at the query>page COMPOSITE grain.
-- Same TRUE-composite reasoning as country>device: stg_gsc_query_page_daily lands query
-- AND page on the SAME row (grain project_id x date x query x page), so impressions is a
-- real per-(query,page) weight. This is the non-additive companion to the fact_daily_kpi
-- 'query>page' additive composite (clicks, impressions) — a card that shows cannibalisation
-- reads BOTH from a single honest source. NEVER stored pre-aggregated (HG-3, it's a VIEW).
-- The `country`/`device` columns are NULL here (this grain has no country/device axis);
-- the shared column list keeps the UNION arity aligned.
SELECT
    project_id,
    date,
    'gsc'                              AS connector,
    'query>page'                       AS breakdown_dimension,
    query || '>' || page               AS breakdown_value,
    CAST(NULL AS VARCHAR)              AS country,
    CAST(NULL AS VARCHAR)              AS device,
    SUM(average_position * impressions) / NULLIF(SUM(impressions), 0) AS average_position,
    SUM(impressions)                   AS impressions_weight,
    MAX(pull_id)                       AS pull_id,
    MAX(loaded_at)                     AS loaded_at
FROM {{ ref('stg_gsc_query_page_daily') }}
GROUP BY project_id, date, query, page
