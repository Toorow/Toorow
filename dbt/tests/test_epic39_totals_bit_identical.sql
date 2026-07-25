-- test_epic39_totals_bit_identical.sql -- Story 39.9 (AC3, E39-NFR06, CRITICAL).
--
-- The no-drift proof, GENERALIZED across the Epic-39 acceptance gate. After the money +
-- timezone modules are enabled, every pre-existing single-currency single-timezone total is
-- bit-for-bit unchanged. Drift threshold = EXACTLY 0 (any nonzero drift is the CRITICAL failure
-- that blocks the story). Two things are proven here on the REAL build:
--
--   PART A -- NAMESPACED ISOLATION (the mechanism that GUARANTEES bit-identity):
--     the validation fixture connectors (__epic39_src_a__/__epic39_src_b__) and its project
--     (__epic39_validation__) must NEVER appear in fact_daily_kpi / cross_source_revenue
--     (these marts UNION module STAGING, not seeds), so the fixture can NEVER perturb any
--     baseline total. If a single namespaced row leaked into a mart, that is a CRITICAL drift.
--
--   PART B -- DECIMAL MONEY BIT-IDENTITY (generalizes test_money_contract_totals_unchanged):
--     for the reference wired DECIMAL money metric (shopify 'revenue', declared native='decimal'
--     in money_metric_units => the read helper's /1e6 branch is NEVER taken), fact_daily_kpi.value
--     equals the value summed straight from its staging source. Drift <> 0 => FAIL. This re-runs
--     39.2's no-drift proof under the full Epic-39 fixture set + mirror_sync, confirming the
--     modules moved NOTHING. (Additional wired decimal-money metrics + cross_source_revenue +
--     semantic_* are covered by their own delivered 39.2 tests, re-run in the same build; this
--     test owns the isolation guarantee + the reference decimal block.)
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

WITH decimal_money AS (
    SELECT canonical_metric
    FROM {{ ref('money_metric_units') }}
    WHERE canonical_native_unit = 'decimal'
),
-- PART A: the namespaced fixture must not have leaked into fact_daily_kpi.
fdk_leak AS (
    SELECT
        project_id,
        date,
        connector,
        metric,
        value AS drift,
        'ISOLATION_FAIL: __epic39_* fixture row leaked into fact_daily_kpi (CRITICAL, would perturb totals)'
            AS failure_reason
    FROM {{ ref('fact_daily_kpi') }}
    WHERE connector LIKE '__epic39_%'
       OR project_id = '__epic39_validation__'
),
-- PART A: the namespaced fixture must not have leaked into cross_source_revenue.
csr_leak AS (
    SELECT
        project_id,
        date,
        CAST(NULL AS VARCHAR) AS connector,
        CAST(NULL AS VARCHAR) AS metric,
        CAST(NULL AS DOUBLE) AS drift,
        'ISOLATION_FAIL: __epic39_* fixture project leaked into cross_source_revenue (CRITICAL)'
            AS failure_reason
    FROM {{ ref('cross_source_revenue') }}
    WHERE project_id = '__epic39_validation__'
),
-- PART B: the reference wired decimal-money block (shopify revenue) is bit-identical.
mart_revenue AS (
    SELECT project_id, date, value AS mart_value
    FROM {{ ref('fact_daily_kpi') }}
    WHERE connector = 'shopify'
      AND metric = 'revenue'
      AND metric IN (SELECT canonical_metric FROM decimal_money)
),
staging_revenue AS (
    SELECT project_id, date, SUM(CAST(revenue AS DOUBLE)) AS staged_value
    FROM {{ ref('stg_shopify_orders_daily') }}
    GROUP BY project_id, date
),
decimal_drift AS (
    SELECT
        m.project_id,
        m.date,
        'shopify' AS connector,
        'revenue' AS metric,
        ABS(m.mart_value - s.staged_value) AS drift,
        'DECIMAL_DRIFT_FAIL: shopify revenue mart total drifted from staging sum (threshold exactly 0)'
            AS failure_reason
    FROM mart_revenue m
    JOIN staging_revenue s
      ON s.project_id = m.project_id AND s.date = m.date
    WHERE ABS(m.mart_value - s.staged_value) <> 0
)
SELECT project_id, date, connector, metric, drift, failure_reason FROM fdk_leak
UNION ALL
SELECT project_id, date, connector, metric, drift, failure_reason FROM csr_leak
UNION ALL
SELECT project_id, date, connector, metric, drift, failure_reason FROM decimal_drift
