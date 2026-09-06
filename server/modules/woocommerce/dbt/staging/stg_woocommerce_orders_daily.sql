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
    -- FX evidence, emitted by ONE macro since the Story 67.13 cutover: the
    -- governed POSED rate is asked first and the seed below is the fallback,
    -- and a refusal (an unanswerable condition, a tie) serves no rate at all.
    -- `fx_as_of_date` is still the day the rate was QUOTED or DECLARED, never
    -- the day its window opens -- story 58.7's repair, carried into both arms.
    {{ toorow_fx_evidence_columns() }}
    raw.orders_count,
    raw.pull_id,
    raw.loaded_at,
    raw.project_id
FROM raw
LEFT JOIN {{ ref('dim_project') }} dp
    ON dp.project_id = raw.project_id
-- ==========================================================================
-- Story 67.13 -- THE GOVERNED RATE IS ASKED FIRST; the seed below is the
-- FALLBACK. Step 4 of the cutover ratified in
-- docs/product-architecture/capabilities/currency-fx.md ("Arbitration,
-- 2026-08-21 -- the read path"), applied to all thirteen staging models in one
-- change because a partial cutover would leave one Project reading the governed
-- store for one connector and the seed for another: two rate authorities inside
-- one total, which is worse than the one wrong authority it replaces.
--
-- The conversion LOCUS does not move. The source currency still stays here and
-- `fx_convert_at_read` still converts once, in the mart. What moves is only
-- where the RATE comes from.
--
-- AT MOST ONE ROW: `toorow_fx_posed_resolution` returns disjoint half-open
-- segments per (project, pair), the winner already chosen by specificity, a tie
-- already REFUSED and an unanswerable condition already named. No QUALIFY and no
-- grain key are needed here, and a plain LEFT JOIN cannot pick a row where the
-- application engine refuses.
--
-- WORDING A (decided 2026-08-22): the declared window governs, retroactively.
-- Nothing below reads when a rate was posted.
LEFT JOIN {{ toorow_fx_posed_resolution('woocommerce') }} fxp
    ON  fxp.project_id     = raw.project_id
   AND fxp.base_currency  = raw.revenue_source_currency
   AND fxp.quote_currency = dp.canonical_currency
   AND CAST(raw.date AS DATE) >= fxp.seg_from
   AND CAST(raw.date AS DATE) <  fxp.seg_until
LEFT JOIN {{ ref('fx_rates') }} fx
    ON fx.from_currency = raw.revenue_source_currency
   AND fx.to_currency   = dp.canonical_currency
   AND CAST(raw.date AS DATE) BETWEEN fx.valid_from AND fx.valid_to
