-- Story 8.11 reconciliation (HARD REQUIREMENT (b), AD-4):
-- For additive metrics, the composite series 'country>device' must total EXACTLY
-- the same per-day value as EACH single-dimension series it is built from
-- ('country' and 'device_category'). This proves the composite does not double-
-- count (over-count) nor drop rows (under-count).
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).
-- Tolerance 0.001 absolute mirrors test_conversions_dedup_rule.sql style but on
-- additive integer-ish sums so any real double count (2x-5x) fails loudly.

WITH composite_totals AS (
    SELECT project_id, date, connector, metric, SUM(value) AS composite_total
    FROM {{ ref('fact_daily_kpi') }}
    WHERE breakdown_dimension = 'country>device'
    GROUP BY project_id, date, connector, metric
),

country_totals AS (
    SELECT project_id, date, connector, metric, SUM(value) AS country_total
    FROM {{ ref('fact_daily_kpi') }}
    WHERE breakdown_dimension = 'country'
    GROUP BY project_id, date, connector, metric
),

device_totals AS (
    SELECT project_id, date, connector, metric, SUM(value) AS device_total
    FROM {{ ref('fact_daily_kpi') }}
    WHERE breakdown_dimension = 'device_category'
    GROUP BY project_id, date, connector, metric
)

SELECT
    c.project_id,
    c.date,
    c.connector,
    c.metric,
    c.composite_total,
    ct.country_total,
    dt.device_total
FROM composite_totals c
JOIN country_totals ct
    ON ct.project_id = c.project_id AND ct.date = c.date
   AND ct.connector = c.connector AND ct.metric = c.metric
JOIN device_totals dt
    ON dt.project_id = c.project_id AND dt.date = c.date
   AND dt.connector = c.connector AND dt.metric = c.metric
WHERE ABS(c.composite_total - ct.country_total) > 0.001
   OR ABS(c.composite_total - dt.device_total) > 0.001
