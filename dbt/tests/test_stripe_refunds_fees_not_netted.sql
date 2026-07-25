-- test_stripe_refunds_fees_not_netted.sql -- Story 15.7 (decision REFERENCE Shopify 15.4).
--
-- Proves the DEDICATED refunds/fees-column decision holds end-to-end: the mart 'revenue' row for
-- stripe is the GROSS charge total, and 'refunds' + 'fees' are SEPARATE positive rows -- neither is
-- subtracted silently from revenue. If some code path had netted refunds or fees into revenue, the
-- mart revenue would be smaller than the gross staging sum on any day carrying a refund/fee, and
-- this test would return that day.
--
-- Guard 1: mart revenue per (project, date) == staging SUM(revenue) for that day (gross).
-- Guard 2: refunds and fees actually LAND as their own mart rows (dedicated columns proven present),
--          so the "net computed explicitly" claim has real operands (AD-9 no-black-box).
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass). Tolerance 0.001.

WITH mart_revenue AS (
    SELECT project_id, date, SUM(value) AS mart_gross_revenue
    FROM {{ ref('fact_daily_kpi') }}
    WHERE connector = 'stripe' AND metric = 'revenue'
    GROUP BY project_id, date
),

staging_gross AS (
    SELECT project_id, date, SUM(revenue) AS staging_gross_revenue
    FROM {{ ref('stg_stripe_payments_daily') }}
    GROUP BY project_id, date
),

-- Guard 1: revenue is gross (refunds/fees never netted in).
gross_mismatch AS (
    SELECT
        m.project_id,
        m.date,
        'revenue_not_gross' AS check_type,
        m.mart_gross_revenue AS mart_value,
        s.staging_gross_revenue AS staging_value
    FROM mart_revenue m
    JOIN staging_gross s
        ON s.project_id = m.project_id AND s.date = m.date
    WHERE ABS(m.mart_gross_revenue - s.staging_gross_revenue) > 0.001
),

-- Guard 2: refunds and fees exist as their own dedicated mart metric rows.
missing_dedicated_metric AS (
    SELECT
        NULL AS project_id,
        NULL AS date,
        'missing_dedicated_metric_' || m.metric AS check_type,
        NULL AS mart_value,
        NULL AS staging_value
    FROM (SELECT 'refunds' AS metric UNION ALL SELECT 'fees') m
    WHERE NOT EXISTS (
        SELECT 1 FROM {{ ref('fact_daily_kpi') }} f
        WHERE f.connector = 'stripe' AND f.metric = m.metric
    )
)

SELECT project_id, date, check_type, mart_value, staging_value FROM gross_mismatch
UNION ALL
SELECT project_id, date, check_type, mart_value, staging_value FROM missing_dedicated_metric
