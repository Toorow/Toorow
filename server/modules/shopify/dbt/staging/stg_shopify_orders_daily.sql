-- Staging: maps raw Shopify order source fields to canonical names.
-- connector-requirements.md Shopify profile (Story 15.4, Epic 15).
-- AD-4: additive metrics only (revenue, refund_amount, orders_count).
-- AD-7: pull_id propagated from raw for provenance chain.
-- Story 15.4: module-owned staging (AI-06 decision Option A -- external model-path).
--
-- GRAIN: one row per ORDER (project_id, date, order_id). transaction_id is a DETAIL
-- dimension carried for the GA4 x Shopify reconciliation (Epic 17) -- it is NOT the
-- grain key and NOT a mart breakdown partition. The mart aggregates orders to the DAY.
--
-- Supersede semantics (AD-7): when several pulls cover the same order
-- (project_id, date, order_id), the LATEST pull wins.
-- ULIDs are lexicographically monotonic, so ORDER BY pull_id DESC = newest first.
-- NB: order_id (not transaction_id) is the supersede key -- a single order can carry
-- several transactions (partial captures/refunds); superseding on transaction_id would
-- drop legitimate multi-transaction detail. Keeping order_id as the grain key means one
-- row per order per pull, with the representative transaction_id for the join.
--
-- REFUNDS (decision de story 15.4, REFERENCE pour Stripe 15.7): refund_amount is a
-- DEDICATED positive column. It is NEVER subtracted silently from revenue here. A net
-- figure is derived explicitly downstream (revenue - refund_amount) so the two flows
-- stay independently auditable (AD-9 no-black-box).
--
-- Currency normalization (AD-6, pattern 4.2, mirrors stg_meta_ads_daily cost):
--   - revenue_source_value / refund_source_value: raw amounts in the order currency
--     (preserved for reconciliation).
--   - revenue_source_currency: order currency from raw column (default 'EUR' for seed).
--   - revenue / refund_amount: normalized to project canonical_currency (EUR by default)
--     via fx_rates seed. Normalization happens ONCE, here in dbt staging (AD-6).
--   When from_currency = to_currency (EUR->EUR), rate = 1.0 -- no conversion.

WITH raw AS (
    SELECT *
    FROM {{ source('raw_shopify', 'raw_shopify_orders') }}
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY project_id, date, order_id
        ORDER BY pull_id DESC
    ) = 1
)

SELECT
    -- Timezone policy: date is the created_at day (source tz boundary accepted at day
    -- grain -- no intraday shift; same policy as stg_meta_ads_daily HG-4).
    raw.date       AS date_source,
    raw.date       AS date,
    raw.order_id,
    raw.transaction_id,
    -- Source amounts preserved (AD-6: reconciliation).
    raw.revenue                                       AS revenue_source_value,
    raw.refund_amount                                 AS refund_source_value,
    raw.revenue_source_currency                       AS revenue_source_currency,
    -- Currency normalization (Story 39.10 FX-at-read [[fx-locus-read-not-staging]]):
    -- Staging preserves immutable source-currency amounts; conversion happens ONCE at read.
    COALESCE(raw.revenue, 0.0)                         AS revenue,
    COALESCE(raw.refund_amount, 0.0)                   AS refund_amount,
    -- FX provenance columns for read-time conversion
    fx.rate                                            AS fx_rate,
    fx.valid_from                                      AS fx_as_of_date,
    'seed'                                             AS fx_source,
    'fixed'                                            AS fx_tier,
    raw.orders_count,
    raw.pull_id,
    raw.loaded_at,
    raw.project_id
FROM raw
-- dim_project supplies canonical_currency per project (AD-6/FR4).
-- COALESCE to 'EUR' when the project row is missing so seeds and tests still pass.
LEFT JOIN {{ ref('dim_project') }} dp
    ON dp.project_id = raw.project_id
-- Story 13.2: FX conflict resolution override (AD-6). Shopify uses revenue_source_currency.
-- target_field = 'revenue' (the canonical field name for Shopify revenue in the dictionary).
LEFT JOIN {{ source('mirror', 'fx_conflict_resolutions') }} fx_res
    ON fx_res.project_id   = raw.project_id
   AND fx_res.target_field = 'revenue'
   AND fx_res.source_module = 'shopify'
-- FX validity window: raw.date is a VARCHAR ISO string (F-06) -- cast for the DATE seed columns.
LEFT JOIN {{ ref('fx_rates') }} fx
    ON fx.from_currency = COALESCE(fx_res.resolved_source_currency, raw.revenue_source_currency)
   AND fx.to_currency   = COALESCE(dp.canonical_currency, 'EUR')
   AND CAST(raw.date AS DATE) BETWEEN fx.valid_from AND fx.valid_to
