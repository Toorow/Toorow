-- transaction_reconciliation: MEASURED Shopify x GA4 sales reconciliation via
-- transaction_id (Epic 17, Story 17.4 / CAP-20 / FR23 -- doc/synthese §3.2).
--
-- ============================ THIS MART IS A READ ============================
-- transaction_reconciliation READS stg_shopify_orders_daily + stg_ga4_transactions_daily
-- only. It creates NO new fact rows and edits NO existing model. fact_daily_kpi /
-- cross_source_conversions / dedup_estimate / customer_journeys are STRICTLY UNTOUCHED
-- (Epic 17 invariant AD-4/AD-6; the "totaux inchanges" discipline of review-epic-10). The
-- transaction grain lives ONLY here and in stg_ga4_transactions_daily -- it NEVER enters
-- fact_daily_kpi (a per-transaction row does not total the connector day, so it is not a
-- breakdown partition; it is a JOIN KEY). This mart is the dedicated home the story brief
-- calls for -- there is deliberately no UNION into the day-grain fact.
--
-- ===================== WHAT THIS MEASURES vs 17.2 (AD-9) =====================
-- 17.2 (dedup_estimate) is an AGGREGATE ESTIMATION: without a user-level join it can only
-- estimate the overlap of regie-claimed conversions against a truth total. THIS mart is a
-- MEASURED RECOVERY RATE: it joins the SAME transaction ids on both sides and counts how
-- many actually match. It is NOT an attribution model (AD-9) -- "N transactions matched"
-- is a data observation, never "GA4 caused the sale". method/estimate_label carry that
-- honesty on every row.
--
-- ============================== TWO GRAINS ==================================
-- 1. DETAIL grain (transaction_reconciliation, this SELECT):
--      one row per (project_id, date, transaction_id) with match_status in
--      ('matched','shopify_only','ga4_only'). A FULL OUTER JOIN of the two sides by
--      (project_id, date, transaction_id).
--    NB: the DAILY aggregate is provided as a SEPARATE model
--    transaction_reconciliation_daily (see that file) so each model has ONE clean grain
--    (AI-05) -- the daily coverage_rate lives there, not smeared across detail rows.
--
-- ===================== F-7 BLIND SPOT (measured honestly) ====================
-- review-15-4 F-7: Shopify transaction_id can be NULL (no sale/capture transaction). A
-- Shopify order WITHOUT a transaction_id CANNOT be reconciled -- it has no join key. We do
-- NOT invent a fallback (any-kind id would corrupt the join). So the coverage_rate
-- denominator is shopify_orders_WITH_txn (orders that CAN be matched), and
-- shopify_orders_without_txn is reported SEPARATELY as the measured blind spot -- never
-- folded into the rate to flatter it. Same on the GA4 side: a GA4 transaction with a NULL
-- id (GA4 '(not set)') is unreconcilable and excluded from the join (filtered in the CTEs).
--
-- ===================== PROVENANCE (AD-9) ====================================
-- shopify_pull_id / ga4_pull_id carried per row so each match is traceable to both source
-- batches. estimate_label = 'measured_reconciliation' (this is a MEASURED overlap, the
-- honest counterpart of dedup_estimate's 'estimation').

{{ config(materialized='view') }}

WITH shopify_txn AS (
    -- One row per Shopify order that HAS a transaction_id (the only orders that can be
    -- reconciled). Orders without a txn are counted separately in the daily rollup (F-7).
    -- An order carries ONE representative transaction_id (15.4 grain: project,date,order),
    -- so grouping by transaction_id collapses at most the rare case of two orders sharing a
    -- representative id; we SUM revenue/orders and keep the provenance MAX.
    SELECT
        project_id,
        date,
        transaction_id,
        SUM(revenue)        AS shopify_revenue,
        SUM(orders_count)   AS shopify_orders,
        MAX(pull_id)        AS shopify_pull_id
    FROM {{ ref('stg_shopify_orders_daily') }}
    WHERE transaction_id IS NOT NULL
    GROUP BY project_id, date, transaction_id
),

ga4_txn AS (
    -- One row per GA4 transaction that HAS an id (unreconcilable NULL-id rows excluded).
    SELECT
        project_id,
        date,
        transaction_id,
        SUM(conversions)       AS ga4_conversions,
        SUM(purchase_revenue)  AS ga4_revenue,
        MAX(pull_id)           AS ga4_pull_id
    FROM {{ ref('stg_ga4_transactions_daily') }}
    WHERE transaction_id IS NOT NULL
    GROUP BY project_id, date, transaction_id
)

SELECT
    -- COALESCE the join keys so FULL OUTER JOIN rows (only one side present) still carry
    -- their project_id/date/transaction_id.
    COALESCE(s.project_id, g.project_id)             AS project_id,
    COALESCE(s.date, g.date)                         AS date,
    COALESCE(s.transaction_id, g.transaction_id)     AS transaction_id,
    -- match_status: matched (both sides), shopify_only (sale not seen in GA4),
    -- ga4_only (GA4 transaction with no matching Shopify order).
    CASE
        WHEN s.transaction_id IS NOT NULL AND g.transaction_id IS NOT NULL THEN 'matched'
        WHEN s.transaction_id IS NOT NULL                                  THEN 'shopify_only'
        ELSE 'ga4_only'
    END                                              AS match_status,
    s.shopify_revenue,
    s.shopify_orders,
    g.ga4_conversions,
    g.ga4_revenue,
    -- AD-9: MEASURED overlap, never an attribution. Constant label on every row.
    'measured_reconciliation'                        AS estimate_label,
    s.shopify_pull_id,
    g.ga4_pull_id
FROM shopify_txn s
FULL OUTER JOIN ga4_txn g
    ON  g.project_id     = s.project_id
    -- review-17-4 F-1 (angle mort ASSUME, non silencieux): la jointure suppose une
    -- frontiere de jour ALIGNEE entre les deux sources. Le date Shopify = jour de
    -- created_at (tz source); le date GA4 = jour dans la TIMEZONE DE LA PROPRIETE.
    -- Une vente a cheval sur minuit en tz divergentes produit un shopify_only (J)
    -- + un ga4_only (J+1) au lieu d'un matched -> coverage_rate SOUS-estime aux
    -- frontieres de journee. Fix Phase B: jointure tolerante +/-1 jour.
    AND g.date           = s.date
    AND g.transaction_id = s.transaction_id
