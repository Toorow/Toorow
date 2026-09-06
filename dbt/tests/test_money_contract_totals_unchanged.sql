-- test_money_contract_totals_unchanged.sql — Story 39.2 (AC3, E39-NFR06, CRITICAL).
--
-- The no-drift proof: the money contract (adapter declaration + canonical-micros read
-- helper + micros-aware semantic views) introduces NO /1e6 (and no other rescale) on any
-- currently-emitted wired money row. Every wired money component today is native_unit=
-- 'decimal' (revenue/cost/refund_amount/… land decimal into fact_daily_kpi: staging keeps the
-- immutable source-currency DECIMAL amount and the mart converts it ONCE through
-- fx_convert_at_read — FX-at-read, Story 39.10 repaired by 48.3, NOT the retired
-- FX-at-staging), so the read helper's /1e6 branch is NEVER taken and fact_daily_kpi
-- stays bit-identical. Story 39.2 does NOT rewrite fact_daily_kpi.sql nor any incumbent
-- staging, so bit-identity holds BY CONSTRUCTION — this test PROVES it structurally.
--
-- Concretely: fact_daily_kpi.value for a 'decimal'-declared money metric must equal the
-- value summed straight from its staging source (no upstream division). We assert it on the
-- shopify 'revenue' block — the reference wired DECIMAL money metric whose staging
-- (stg_shopify_orders_daily) sums cleanly to the mart (mirrors test_shopify_totals_isolated,
-- test_meta_cost_normalization idioms). Drift threshold = EXACTLY 0 (any nonzero drift is the
-- CRITICAL failure that blocks the story — unlike the 0.01% tolerance used for FX float checks,
-- here the answer must be exactly "the contract moved nothing").
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

WITH decimal_money AS (
    -- The money metrics the contract declares native_unit='decimal' (from the seed). A
    -- 'decimal' money row must be summed as-is into the mart — NEVER pre-divided by 1e6.
    SELECT canonical_metric
    FROM {{ ref('money_metric_units') }}
    WHERE canonical_native_unit = 'decimal'
),
mart_revenue AS (
    SELECT project_id, date, value AS mart_value
    FROM {{ ref('fact_daily_kpi') }}
    WHERE connector = 'shopify'
      AND metric = 'revenue'
      AND metric IN (SELECT canonical_metric FROM decimal_money)
),
staging_revenue AS (
    -- DECIMAL, not DOUBLE (repaired 2026-08-04, twin of the same line in
    -- test_epic39_totals_bit_identical.sql). Story 48.3 made the mart side exact;
    -- summing DOUBLE here forced it back through a float and manufactured a
    -- 2.3e-13 drift on 41 days against a threshold of exactly 0. Both sides exact
    -- => the threshold can stay at 0, which is what makes this test worth having.
    SELECT project_id, date, SUM(CAST(revenue AS {{ toorow_decimal_type(38, 9) }})) AS staged_value
    FROM {{ ref('stg_shopify_orders_daily') }}
    GROUP BY project_id, date
)
SELECT
    m.project_id,
    m.date,
    m.mart_value,
    s.staged_value,
    ABS(m.mart_value - s.staged_value) AS drift
FROM mart_revenue m
JOIN staging_revenue s
  ON s.project_id = m.project_id AND s.date = m.date
WHERE ABS(m.mart_value - s.staged_value) <> 0
