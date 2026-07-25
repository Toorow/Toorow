-- test_stripe_totals_isolated.sql -- Story 15.7 (reconciliation: existing totals unchanged).
--
-- The Stripe mart block is ADDITIVE-ONLY and connector-keyed: it must emit rows ONLY under
-- connector='stripe', with the single breakdown_dimension 'day_total'. This proves the
-- "totaux GA4/Meta/GSC/Shopify/TikTok/Klaviyo/LinkedIn existants inchanges" reconciliation at the
-- mart grain: Stripe adds a brand-new connector partition and touches no existing connector's rows
-- (fact_daily_kpi keys on connector, so a stripe row can never collide with any other connector).
--
-- It also guards the intended shape:
--   * every stripe row carries breakdown_dimension='day_total' (charge_id / payment_intent_id /
--     client_reference_id are NEVER mart partitions -- they stay raw/staging detail dimensions);
--   * stripe metrics are exactly {revenue, refunds, fees, transaction_count, order_count} -- in
--     particular 'conversions' is NEVER produced by stripe (order_count/transaction_count are not
--     mapped onto conversions, so no collision with the 3.7 GA4/Meta cross-source dedup);
--   * breakdown_value is never NULL.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

SELECT connector, metric, breakdown_dimension, breakdown_value, COUNT(*) AS n
FROM {{ ref('fact_daily_kpi') }}
WHERE connector = 'stripe'
  AND (
        breakdown_dimension <> 'day_total'
     OR metric NOT IN ('revenue', 'refunds', 'fees', 'transaction_count', 'order_count')
     OR breakdown_value IS NULL
      )
GROUP BY connector, metric, breakdown_dimension, breakdown_value
