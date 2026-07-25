-- test_shopify_refund_not_netted.sql — Story 15.4 (decision REFERENCE pour Stripe 15.7).
--
-- Proves the DEDICATED-refund-column decision holds end-to-end: the mart 'revenue' row
-- for shopify is the GROSS order total, and 'refund_amount' is a SEPARATE positive row --
-- refunds are NEVER subtracted silently from revenue.
--
-- Guard: the mart revenue value for a (project, date) must equal the staging SUM of
-- revenue for that day (gross, refunds untouched). If some code path had netted refunds
-- into revenue, the mart revenue would be smaller than the gross staging sum on any day
-- carrying a refund, and this test would return that day.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass). Tolerance 0.001.

WITH mart_revenue AS (
    SELECT project_id, date, SUM(value) AS mart_gross_revenue
    FROM {{ ref('fact_daily_kpi') }}
    WHERE connector = 'shopify' AND metric = 'revenue'
    GROUP BY project_id, date
),

staging_gross AS (
    SELECT project_id, date, SUM(revenue) AS staging_gross_revenue
    FROM {{ ref('stg_shopify_orders_daily') }}
    GROUP BY project_id, date
)

SELECT
    m.project_id,
    m.date,
    m.mart_gross_revenue,
    s.staging_gross_revenue
FROM mart_revenue m
JOIN staging_gross s
    ON s.project_id = m.project_id AND s.date = m.date
WHERE ABS(m.mart_gross_revenue - s.staging_gross_revenue) > 0.001
