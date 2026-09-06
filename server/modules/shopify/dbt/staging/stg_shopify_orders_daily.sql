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
--   - revenue / refund_amount: the SAME source-currency amounts, converted at read.
--   NOTHING IS NORMALIZED HERE, and the two lines above that said otherwise were
--   read as fact for one epic. Story 39.10 moved the conversion to the READ
--   ([[fx-locus-read-not-staging]]): this model preserves the source-currency
--   amount and CARRIES the rate provenance beside it; `fx_convert_at_read` in the
--   mart is what converts, once, and it can be re-derived after a policy change.

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
-- dim_project supplies canonical_currency per project (AD-6/FR4).
-- Story 48.3: NO 'EUR' fallback. A Project that has not confirmed a reporting
-- currency joins no rate, so fx_rate stays NULL, fx_convert_at_read yields NULL and
-- fx_gap_code says why -- instead of a source-currency amount being summed into a
-- EUR total as though a dollar were a euro.
LEFT JOIN {{ ref('dim_project') }} dp
    ON dp.project_id = raw.project_id
-- Story 13.2: FX conflict resolution override (AD-6). Shopify uses revenue_source_currency.
-- target_field = 'revenue' (the canonical field name for Shopify revenue in the dictionary).
LEFT JOIN {{ toorow_source_or_empty('mirror', 'fx_source_currency_bindings', [
        ['project_id', 'string'],
        ['target_field', 'string'],
        ['source_module', 'string'],
        ['resolved_source_currency', 'string'],
    ]) }} fx_res
    ON fx_res.project_id   = raw.project_id
   AND fx_res.target_field = 'revenue'
   AND fx_res.source_module = 'shopify'
-- FX validity window: raw.date is a VARCHAR ISO string (F-06) -- cast for the DATE seed columns.
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
LEFT JOIN {{ toorow_fx_posed_resolution('shopify') }} fxp
    ON  fxp.project_id     = raw.project_id
   AND fxp.base_currency  = COALESCE(fx_res.resolved_source_currency, raw.revenue_source_currency)
   AND fxp.quote_currency = dp.canonical_currency
   AND CAST(raw.date AS DATE) >= fxp.seg_from
   AND CAST(raw.date AS DATE) <  fxp.seg_until
LEFT JOIN {{ ref('fx_rates') }} fx
    ON fx.from_currency = COALESCE(fx_res.resolved_source_currency, raw.revenue_source_currency)
   AND fx.to_currency   = dp.canonical_currency
   AND CAST(raw.date AS DATE) BETWEEN fx.valid_from AND fx.valid_to
