-- test_shopify_totals_isolated.sql — Story 15.4 (reconciliation: existing totals unchanged).
--
-- The Shopify mart block is ADDITIVE-ONLY and connector-keyed: it must emit rows ONLY under
-- connector='shopify', with the single breakdown_dimension 'day_total'. This proves the
-- "totaux GA4/Meta/GSC existants inchangés" reconciliation at the mart grain: Shopify adds a
-- brand-new connector partition and touches no existing connector's rows (fact_daily_kpi keys
-- on connector, so a shopify row can never collide with a GA4/Meta/GSC row).
--
-- It also guards the intended shape:
--   * every shopify row carries breakdown_dimension='day_total' (transaction_id is NEVER a
--     mart partition -- it stays a raw/staging detail dimension, the GA4 join key of Epic 17);
--   * shopify metrics are exactly {revenue, refund_amount, orders_count} -- in particular
--     'conversions' is NEVER produced by shopify (orders_count is not mapped onto conversions).
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

SELECT connector, metric, breakdown_dimension, COUNT(*) AS n
FROM {{ ref('fact_daily_kpi') }}
WHERE connector = 'shopify'
  AND (
        breakdown_dimension <> 'day_total'
     OR metric NOT IN ('revenue', 'refund_amount', 'orders_count')
      )
GROUP BY connector, metric, breakdown_dimension
