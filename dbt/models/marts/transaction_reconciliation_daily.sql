-- transaction_reconciliation_daily: per-(project,date) rollup of the MEASURED Shopify x
-- GA4 transaction reconciliation (Epic 17, Story 17.4 -- doc/synthese §3.2).
--
-- ============================ THIS MART IS A READ ============================
-- Reads transaction_reconciliation (the detail-grain view) + stg_shopify_orders_daily
-- (to count the F-7 blind spot of orders WITHOUT a transaction_id). Edits NO existing
-- model -- fact_daily_kpi / cross_source_conversions / dedup_estimate are UNTOUCHED
-- (Epic 17 invariant AD-4/AD-6). The transaction grain never enters fact_daily_kpi.
--
-- GRAIN (enforced by transaction_reconciliation_daily_grain_unique):
--   one row per (project_id, date).
--
-- ===================== COVERAGE RATE (doc/synthese §3.2, AD-9) ===============
--   coverage_rate = matched_count / NULLIF(shopify_orders_with_txn, 0)
-- This is the story's Given/When/Then: 100 Shopify sales, 85 with a transaction_id found
-- in GA4 -> coverage_rate = 85 / 85_or_more... precisely: the epic AC reads 85% of the
-- Shopify orders reconciled. We express it as matched / shopify_orders_with_txn -- the
-- share of RECONCILABLE Shopify sales that GA4 confirms. coverage_rate is NULL (not 0)
-- when there are zero Shopify orders with a txn (NULLIF guard) -- an honest "cannot
-- measure", never a disguised 0% (AD-9). This is a MEASURED recovery rate, NOT an
-- attribution (method = 'measured_reconciliation').
--
-- ===================== F-7 BLIND SPOT (measured, not hidden) =================
-- shopify_orders_without_txn is the count of Shopify orders that carry NO transaction_id
-- (review-15-4 F-7: no any-kind fallback). These orders CANNOT be reconciled -- they are
-- EXCLUDED from the coverage_rate denominator (which is orders WITH a txn) and reported
-- SEPARATELY so the blind spot is visible, never folded in to flatter the rate.
--
-- ===================== METHOD / DEGRADE (AD-9) ==============================
-- method: 'measured_reconciliation' -- distinguishes this from 17.2's aggregate
-- 'estimation'. The 17.3 card reads method to say "reconciliee par transaction (X%)" vs
-- "estimation aggregee". When GA4 has NO transactions for the day (e-commerce not tracked
-- / lead-gen site), ga4_transactions = 0, matched_count = 0, coverage_rate = 0 over a
-- non-zero denominator (an honest "0% confirmed", the site has sales GA4 did not see) OR
-- NULL if there are also no reconcilable Shopify orders. Both are honest; neither invents.
--
-- ===================== PROVENANCE (AD-9) ====================================
-- shopify_pull_id / ga4_pull_id = MAX per day across the detail rows (both sides).

{{ config(materialized='view') }}

WITH detail AS (
    SELECT *
    FROM {{ ref('transaction_reconciliation') }}
),

-- Per-(project,date) counts from the detail grain.
agg AS (
    SELECT
        project_id,
        date,
        -- Shopify orders that HAVE a transaction_id = matched + shopify_only.
        SUM(CASE WHEN match_status IN ('matched', 'shopify_only') THEN 1 ELSE 0 END)
            AS shopify_orders_with_txn,
        -- GA4 transactions (with an id) = matched + ga4_only.
        SUM(CASE WHEN match_status IN ('matched', 'ga4_only') THEN 1 ELSE 0 END)
            AS ga4_transactions,
        SUM(CASE WHEN match_status = 'matched' THEN 1 ELSE 0 END) AS matched_count,
        MAX(shopify_pull_id) AS shopify_pull_id,
        MAX(ga4_pull_id)     AS ga4_pull_id
    FROM detail
    GROUP BY project_id, date
),

-- F-7 blind spot: Shopify orders WITHOUT a transaction_id, counted straight from staging
-- (these never reach the detail view -- they have no join key). Reported separately.
shopify_no_txn AS (
    SELECT
        project_id,
        date,
        SUM(CASE WHEN transaction_id IS NULL THEN orders_count ELSE 0 END)
            AS shopify_orders_without_txn
    FROM {{ ref('stg_shopify_orders_daily') }}
    GROUP BY project_id, date
)

SELECT
    a.project_id,
    a.date,
    a.shopify_orders_with_txn,
    a.ga4_transactions,
    a.matched_count,
    -- coverage_rate = matched / reconcilable Shopify orders. NULL (never 0) when there are
    -- no reconcilable Shopify orders -- honest "cannot measure" (AD-9 / F-7).
    a.matched_count::DOUBLE / NULLIF(a.shopify_orders_with_txn, 0) AS coverage_rate,
    -- F-7 blind spot, measured separately (never folded into the rate).
    COALESCE(n.shopify_orders_without_txn, 0)        AS shopify_orders_without_txn,
    -- AD-9: this is a MEASURED overlap, not an aggregate estimate and not an attribution.
    'measured_reconciliation'                        AS method,
    a.shopify_pull_id,
    a.ga4_pull_id
FROM agg a
LEFT JOIN shopify_no_txn n
    ON n.project_id = a.project_id AND n.date = a.date
