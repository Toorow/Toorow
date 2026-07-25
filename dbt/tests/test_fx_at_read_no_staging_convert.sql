-- Singular test (0 rows = PASS) (Story 39.10 / AC1).
-- Asserts that migrated staging models no longer perform FX multiply in staging.
-- In staging, the canonical cost/revenue column must equal source_value.

WITH meta_check AS (
    SELECT 'meta-ads' AS connector, cost, cost_source_value
    FROM {{ ref('stg_meta_ads_daily') }}
    WHERE cost != cost_source_value
),
shopify_check AS (
    SELECT 'shopify' AS connector, revenue, revenue_source_value
    FROM {{ ref('stg_shopify_orders_daily') }}
    WHERE revenue != revenue_source_value
),
stripe_check AS (
    SELECT 'stripe' AS connector, revenue, revenue_source_value
    FROM {{ ref('stg_stripe_payments_daily') }}
    WHERE revenue != revenue_source_value
),
square_check AS (
    SELECT 'square' AS connector, revenue, revenue_source_value
    FROM {{ ref('stg_square_payments_daily') }}
    WHERE revenue != revenue_source_value
),
woo_check AS (
    SELECT 'woocommerce' AS connector, revenue, revenue_source_value
    FROM {{ ref('stg_woocommerce_orders_daily') }}
    WHERE revenue != revenue_source_value
),
tiktok_check AS (
    SELECT 'tiktok-ads' AS connector, cost, cost_source_value
    FROM {{ ref('stg_tiktok_ads_daily') }}
    WHERE cost != cost_source_value
),
linkedin_check AS (
    SELECT 'linkedin-ads' AS connector, cost, cost_source_value
    FROM {{ ref('stg_linkedin_ads_campaign_daily') }}
    WHERE cost != cost_source_value
)

SELECT connector FROM meta_check
UNION ALL
SELECT connector FROM shopify_check
UNION ALL
SELECT connector FROM stripe_check
UNION ALL
SELECT connector FROM square_check
UNION ALL
SELECT connector FROM woo_check
UNION ALL
SELECT connector FROM tiktok_check
UNION ALL
SELECT connector FROM linkedin_check
