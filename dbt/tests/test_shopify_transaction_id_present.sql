-- test_shopify_transaction_id_present.sql — Story 15.4 (AC quality gate).
--
-- Proves transaction_id is present (non-null) on >= 95% of Shopify orders in the
-- staging model. transaction_id is the GA4 x Shopify reconciliation key (Epic 17);
-- a low presence rate would silently weaken the dedup join, so the pipeline must fail
-- loudly if coverage drops below the calibrated 95% threshold.
--
-- AI-53: the exact API field (order.transactions[].id) and its availability are to be
-- confirmed live (Phase B). This test calibrates the quality gate on the SEED payload,
-- which is generated with transaction_id present on >= 95% of orders by design.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass). This test returns
-- one row ONLY when the presence ratio is below 0.95.

WITH coverage AS (
    SELECT
        COUNT(*)                                                        AS total_orders,
        COUNT(transaction_id)                                           AS with_txn,
        CAST(COUNT(transaction_id) AS DOUBLE) / NULLIF(COUNT(*), 0)     AS present_ratio
    FROM {{ ref('stg_shopify_orders_daily') }}
)

SELECT total_orders, with_txn, present_ratio
FROM coverage
WHERE total_orders > 0
  AND present_ratio < 0.95
