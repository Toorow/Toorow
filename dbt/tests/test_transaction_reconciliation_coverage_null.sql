-- Story 17.4 honest degrade (AD-9): coverage_rate must be NULL -- never a disguised 0 --
-- whenever there are zero reconcilable Shopify orders (shopify_orders_with_txn = 0). The
-- NULLIF(shopify_orders_with_txn, 0) guard in transaction_reconciliation_daily.sql enforces
-- this; this test proves it never regresses to a fabricated 0% or a division error.
--
-- Conversely, a day WITH reconcilable Shopify orders MUST carry a non-NULL coverage_rate
-- in [0, 1] (matched_count <= shopify_orders_with_txn by construction: matched is a subset).
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

WITH d AS (
    SELECT
        project_id,
        date,
        shopify_orders_with_txn,
        matched_count,
        coverage_rate
    FROM {{ ref('transaction_reconciliation_daily') }}
)

-- Violation 1: no reconcilable orders but coverage_rate is NOT NULL (disguised 0).
SELECT project_id, date, 'coverage_not_null_when_no_reconcilable_orders' AS violation
FROM d
WHERE shopify_orders_with_txn = 0
  AND coverage_rate IS NOT NULL

UNION ALL

-- Violation 2: reconcilable orders exist but coverage_rate is NULL or out of [0, 1].
SELECT project_id, date, 'coverage_null_or_out_of_range_with_orders' AS violation
FROM d
WHERE shopify_orders_with_txn > 0
  AND (coverage_rate IS NULL OR coverage_rate < 0 OR coverage_rate > 1)
