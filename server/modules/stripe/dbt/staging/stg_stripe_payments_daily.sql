-- Staging: maps raw Stripe payment source fields to canonical names.
-- connector-requirements.md Stripe profile (Story 15.7, Epic 15).
-- AD-4: additive metrics only (revenue, refunds, fees, transaction_count, order_count).
-- AD-7: pull_id propagated from raw for provenance chain.
-- Story 15.7: module-owned staging (AI-06 decision Option A -- external model-path).
--
-- GRAIN: one row per CHARGE (project_id, date, charge_id). payment_intent_id and
-- client_reference_id are DETAIL dimensions carried for reconciliation -- NOT the grain key
-- and NOT mart breakdown partitions. The mart aggregates charges to the DAY.
--
-- Supersede semantics (AD-7): when several pulls cover the same charge
-- (project_id, date, charge_id), the LATEST pull wins.
-- ULIDs are lexicographically monotonic, so ORDER BY pull_id DESC = newest first.
--
-- MONTANTS (decision de story 15.7): les montants sont deja convertis EN UNITES devise
-- par le connecteur (_amount_to_units : centimes Stripe -> unites, conversion EXPLICITE au
-- pull). Le staging ne re-divise PAS ; il applique UNIQUEMENT la normalisation FX projet
-- (pattern 4.2). revenue / refunds / fees sont des COLONNES DEDIEES positives -- refunds et
-- fees ne sont JAMAIS soustraits silencieusement de revenue (net calcule explicitement en aval,
-- AD-9 no-black-box), meme discipline que refund_amount de Shopify 15.4.
--
-- REGLE DE DEDUP REVENUE (CRITIQUE, AD-4) : revenue Stripe et revenue Shopify peuvent mesurer
-- la MEME vente. Ils ne sont JAMAIS sommes dans un total croise -- la vue cross_source_revenue
-- (miroir de cross_source_conversions 3.7) choisit UNE source gagnante par (projet, jour) via
-- metric_source_priority.csv (revenue -> shopify priorite 1, stripe priorite 2). Les lignes par
-- source restent independamment interrogeables dans fact_daily_kpi.
--
-- Currency normalization (AD-6, pattern 4.2, mirrors stg_shopify_orders_daily / stg_meta_ads_daily):
--   - revenue_source_value / refunds_source_value / fees_source_value: raw amounts in the
--     charge currency (preserved for reconciliation).
--   - revenue_source_currency: charge currency from raw column (default 'EUR' for seed).
--   - revenue / refunds / fees: the SAME source-currency amounts, converted at read.
--   NOTHING IS NORMALIZED HERE, and the two lines above that said otherwise were
--   read as fact for one epic. Story 39.10 moved the conversion to the READ
--   ([[fx-locus-read-not-staging]]): this model preserves the source-currency
--   amount and CARRIES the rate provenance beside it; `fx_convert_at_read` in the
--   mart is what converts, once, and it can be re-derived after a policy change.

WITH raw AS (
    SELECT *
    FROM {{ source('raw_stripe', 'raw_stripe_payments') }}
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY project_id, date, charge_id
        ORDER BY pull_id DESC
    ) = 1
)

SELECT
    -- Timezone policy: date is the charge created day (UTC boundary accepted at day grain --
    -- no intraday shift; same policy as stg_shopify_orders_daily / stg_meta_ads_daily HG-4).
    raw.date       AS date_source,
    raw.date       AS date,
    raw.charge_id,
    raw.payment_intent_id,
    -- client_reference_id: GA4-joinable id WHEN present (souvent absent -- AI-53). Detail
    -- dimension carried for the aggregate-level Epic 17 reconciliation, NOT a mart partition.
    raw.client_reference_id,
    -- Source amounts preserved (AD-6: reconciliation). Already in currency units (converted
    -- from centimes by the connector at pull time -- staging does NOT re-divide).
    raw.revenue                                        AS revenue_source_value,
    raw.refunds                                        AS refunds_source_value,
    raw.fees                                           AS fees_source_value,
    raw.revenue_source_currency                        AS revenue_source_currency,
    -- Currency normalization (Story 39.10 FX-at-read [[fx-locus-read-not-staging]]):
    -- Staging preserves immutable source-currency amounts; conversion happens ONCE at read.
    COALESCE(raw.revenue, 0.0)                         AS revenue,
    COALESCE(raw.refunds, 0.0)                         AS refunds,
    COALESCE(raw.fees, 0.0)                            AS fees,
    -- FX evidence, emitted by ONE macro since the Story 67.13 cutover: the
    -- governed POSED rate is asked first and the seed below is the fallback,
    -- and a refusal (an unanswerable condition, a tie) serves no rate at all.
    -- `fx_as_of_date` is still the day the rate was QUOTED or DECLARED, never
    -- the day its window opens -- story 58.7's repair, carried into both arms.
    {{ toorow_fx_evidence_columns() }}
    raw.transaction_count,
    raw.order_count,
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
-- Story 13.2: FX conflict resolution override (AD-6). Stripe uses revenue_source_currency.
-- target_field = 'revenue' (canonical field name for Stripe revenue in the dictionary).
LEFT JOIN {{ toorow_source_or_empty('mirror', 'fx_source_currency_bindings', [
        ['project_id', 'string'],
        ['target_field', 'string'],
        ['source_module', 'string'],
        ['resolved_source_currency', 'string'],
    ]) }} fx_res
    ON fx_res.project_id   = raw.project_id
   AND fx_res.target_field = 'revenue'
   AND fx_res.source_module = 'stripe'
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
LEFT JOIN {{ toorow_fx_posed_resolution('stripe') }} fxp
    ON  fxp.project_id     = raw.project_id
   AND fxp.base_currency  = COALESCE(fx_res.resolved_source_currency, raw.revenue_source_currency)
   AND fxp.quote_currency = dp.canonical_currency
   AND CAST(raw.date AS DATE) >= fxp.seg_from
   AND CAST(raw.date AS DATE) <  fxp.seg_until
LEFT JOIN {{ ref('fx_rates') }} fx
    ON fx.from_currency = COALESCE(fx_res.resolved_source_currency, raw.revenue_source_currency)
   AND fx.to_currency   = dp.canonical_currency
   AND CAST(raw.date AS DATE) BETWEEN fx.valid_from AND fx.valid_to
