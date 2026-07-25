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
--   - revenue / refunds / fees: normalized to project canonical_currency (EUR by default)
--     via fx_rates seed. Normalization happens ONCE, here in dbt staging (AD-6).
--   When from_currency = to_currency (EUR->EUR), rate = 1.0 -- no conversion.

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
    -- FX provenance columns for read-time conversion
    fx.rate                                            AS fx_rate,
    fx.valid_from                                      AS fx_as_of_date,
    'seed'                                             AS fx_source,
    'fixed'                                            AS fx_tier,
    raw.transaction_count,
    raw.order_count,
    raw.pull_id,
    raw.loaded_at,
    raw.project_id
FROM raw
-- dim_project supplies canonical_currency per project (AD-6/FR4).
-- COALESCE to 'EUR' when the project row is missing so seeds and tests still pass.
LEFT JOIN {{ ref('dim_project') }} dp
    ON dp.project_id = raw.project_id
-- Story 13.2: FX conflict resolution override (AD-6). Stripe uses revenue_source_currency.
-- target_field = 'revenue' (canonical field name for Stripe revenue in the dictionary).
LEFT JOIN {{ source('mirror', 'fx_conflict_resolutions') }} fx_res
    ON fx_res.project_id   = raw.project_id
   AND fx_res.target_field = 'revenue'
   AND fx_res.source_module = 'stripe'
-- FX validity window: raw.date is a VARCHAR ISO string (F-06) -- cast for the DATE seed columns.
LEFT JOIN {{ ref('fx_rates') }} fx
    ON fx.from_currency = COALESCE(fx_res.resolved_source_currency, raw.revenue_source_currency)
   AND fx.to_currency   = COALESCE(dp.canonical_currency, 'EUR')
   AND CAST(raw.date AS DATE) BETWEEN fx.valid_from AND fx.valid_to
