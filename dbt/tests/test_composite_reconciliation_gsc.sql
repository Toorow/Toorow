-- AI-51 (Epic 8) GSC composite reconciliation (HARD REQUIREMENT (b), AD-4):
-- For additive GSC metrics (clicks, impressions), the composite series
-- 'country>device' must total EXACTLY the same per-day value as EACH single-
-- dimension series it is built from ('country' and 'device'). This proves the
-- composite does not double-count (over-count) nor drop rows (under-count).
--
-- NOTE vs test_composite_reconciliation.sql (GA4): GA4's mono device series is
-- named 'device_category'; GSC's is named 'device'. That GA4 test JOINs on
-- breakdown_dimension='device_category', which GSC rows never carry, so GSC is NOT
-- covered by it -- this connector-scoped test closes that gap.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).
-- Tolerance 0.001 absolute: additive integer-ish sums, so any real double count
-- (2x-5x) fails loudly.

WITH composite_totals AS (
    SELECT project_id, date, metric, SUM(value) AS composite_total
    FROM {{ ref('fact_daily_kpi') }}
    WHERE connector = 'gsc' AND breakdown_dimension = 'country>device'
    GROUP BY project_id, date, metric
),

country_totals AS (
    SELECT project_id, date, metric, SUM(value) AS country_total
    FROM {{ ref('fact_daily_kpi') }}
    WHERE connector = 'gsc' AND breakdown_dimension = 'country'
    GROUP BY project_id, date, metric
),

device_totals AS (
    SELECT project_id, date, metric, SUM(value) AS device_total
    FROM {{ ref('fact_daily_kpi') }}
    WHERE connector = 'gsc' AND breakdown_dimension = 'device'
    GROUP BY project_id, date, metric
)

SELECT
    c.project_id,
    c.date,
    c.metric,
    c.composite_total,
    ct.country_total,
    dt.device_total
FROM composite_totals c
JOIN country_totals ct
    ON ct.project_id = c.project_id AND ct.date = c.date AND ct.metric = c.metric
JOIN device_totals dt
    ON dt.project_id = c.project_id AND dt.date = c.date AND dt.metric = c.metric
WHERE ABS(c.composite_total - ct.country_total) > 0.001
   OR ABS(c.composite_total - dt.device_total) > 0.001
