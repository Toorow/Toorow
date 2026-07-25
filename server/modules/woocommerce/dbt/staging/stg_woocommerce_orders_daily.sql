-- Staging: maps raw WooCommerce order source fields to canonical names.
-- WooCommerce profile (epic-25, self-hosted commerce sibling of shopify).
-- AD-4: additive metrics only (revenue, refund_amount, orders_count).
-- AD-7: pull_id propagated from raw for the provenance chain.
-- Module-owned staging (AI-06 Option A -- external model-path).
--
-- GRAIN: one row per ORDER (project_id, date, order_id). transaction_id is a DETAIL
-- dimension carried for GA4 x WooCommerce reconciliation (Epic 17) -- NOT the grain key
-- and NOT a mart breakdown partition. The mart aggregates orders to the DAY.
--
-- Supersede semantics (AD-7): when several pulls cover the same order
-- (project_id, date, order_id), the LATEST pull wins. ULIDs are lexicographically
-- monotonic, so ORDER BY pull_id DESC = newest first.
--
-- REFUNDS (decision REFERENCE shopify 15.4 / stripe 15.7): refund_amount is a
-- DEDICATED positive column (WooCommerce reports refunds as negative; the connector
-- already took abs() at ingestion). It is NEVER subtracted silently from revenue here;
-- a net figure (revenue - refund_amount) is derived explicitly downstream (AD-9).
--
-- Currency normalization (AD-6, pattern 4.2, mirrors stg_shopify_orders_daily):
--   revenue / refund_amount are normalized to the project canonical_currency (EUR by
--   default) via the fx_rates seed; source values preserved for reconciliation.
--   EUR->EUR rate = 1.0 (no conversion).

WITH raw AS (
    SELECT *
    FROM {{ source('raw_woocommerce', 'raw_woocommerce_orders') }}
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY project_id, date, order_id
        ORDER BY pull_id DESC
    ) = 1
)

SELECT
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
LEFT JOIN {{ ref('dim_project') }} dp
    ON dp.project_id = raw.project_id
LEFT JOIN {{ ref('fx_rates') }} fx
    ON fx.from_currency = raw.revenue_source_currency
   AND fx.to_currency   = COALESCE(dp.canonical_currency, 'EUR')
   AND CAST(raw.date AS DATE) BETWEEN fx.valid_from AND fx.valid_to
