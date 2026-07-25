-- semantic_ctr: Click-Through Rate view (Story 4.1, AC7)
-- CTR = clicks / impressions (ratio metric, AD-4)
--
-- This view is materialized as a VIEW, never as a table (HG-3).
-- Ratio metrics are NOT stored in fact_daily_kpi (AD-4 enforcement).
-- The fact_kpi_metric_additive_only test verifies fact_daily_kpi stays ratio-free.
--
-- HG-1 (AD-2): no module-specific strings in this file.
-- Revenue note: if no module supplies 'clicks' or 'impressions', returns NULL rows (NULLIF).

{{ config(materialized='view') }}

SELECT
    project_id,
    date,
    connector,
    breakdown_dimension,
    breakdown_value,
    SUM(CASE WHEN metric = 'clicks' THEN value ELSE 0 END)
    / NULLIF(SUM(CASE WHEN metric = 'impressions' THEN value ELSE 0 END), 0) AS ctr,
    SUM(CASE WHEN metric = 'clicks' THEN value ELSE 0 END) AS semantic_numerator,
    SUM(CASE WHEN metric = 'impressions' THEN value ELSE 0 END) AS semantic_denominator,
    MAX(pull_id) AS pull_id
FROM {{ ref('fact_daily_kpi') }}
GROUP BY project_id, date, connector, breakdown_dimension, breakdown_value
