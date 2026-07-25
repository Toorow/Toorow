-- Staging: maps raw Square payment source fields to canonical names.
-- AD-4: additive metrics only (revenue, refunds, fees, transaction_count, order_count).
-- AD-7: pull_id propagated from raw for provenance chain.
-- Module-owned staging (AI-06 decision Option A -- external model-path), miroir Stripe 15.7.
--
-- GRAIN: one row per PAYMENT (project_id, date, payment_id). order_id and location_id are
-- DETAIL dimensions carried for reconciliation / topology -- NOT the grain key and NOT mart
-- breakdown partitions. The mart aggregates payments to the DAY.
--
-- Supersede semantics (AD-7): when several pulls cover the same payment
-- (project_id, date, payment_id), the LATEST pull wins. ULIDs are lexicographically
-- monotonic, so ORDER BY pull_id DESC = newest first.
--
-- MONTANTS: deja convertis EN UNITES devise par le connecteur (_amount_to_units : centimes
-- Square -> unites, conversion EXPLICITE au pull). Le staging ne re-divise PAS ; il applique
-- UNIQUEMENT la normalisation FX projet (pattern 4.2). revenue / refunds / fees sont des
-- COLONNES DEDIEES positives -- refunds et fees ne sont JAMAIS soustraits silencieusement de
-- revenue (net calcule explicitement en aval, AD-9 no-black-box), meme discipline que Stripe 15.7.
--
-- REGLE DE DEDUP REVENUE (CRITIQUE, AD-4) : revenue Square, Shopify et Stripe peuvent mesurer
-- la MEME vente. Ils ne sont JAMAIS sommes dans un total croise -- la vue cross_source_revenue
-- choisit UNE source gagnante par (projet, jour) via metric_source_priority.csv (revenue ->
-- shopify 1, stripe 2, square 3). Les lignes par source restent independamment interrogeables.
--
-- Currency normalization (AD-6, pattern 4.2, mirrors stg_stripe_payments_daily):
--   - revenue_source_value / refunds_source_value / fees_source_value: raw amounts in the
--     payment currency (preserved for reconciliation).
--   - revenue_source_currency: payment currency from raw column (default 'USD' for seed).
--   - revenue / refunds / fees: normalized to project canonical_currency via fx_rates seed.
--   When from_currency = to_currency, rate = 1.0 -- no conversion.

WITH raw AS (
    SELECT *
    FROM {{ source('raw_square', 'raw_square_payments') }}
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY project_id, date, payment_id
        ORDER BY pull_id DESC
    ) = 1
)

SELECT
    -- Timezone policy: date is the payment created day (UTC boundary accepted at day grain --
    -- no intraday shift; same policy as stg_stripe_payments_daily HG-4).
    raw.date       AS date_source,
    raw.date       AS date,
    raw.payment_id,
    -- order_id: Square Order id, joignable Orders API WHEN present. Detail dimension carried
    -- for the aggregate-level Epic 17 reconciliation, NOT a mart partition. Nullable.
    raw.order_id,
    -- location_id: emplacement du paiement (topologie). Detail dimension, NOT a mart partition.
    raw.location_id,
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
-- FX validity window: raw.date is a VARCHAR ISO string (F-06) -- cast for the DATE seed columns.
LEFT JOIN {{ ref('fx_rates') }} fx
    ON fx.from_currency = raw.revenue_source_currency
   AND fx.to_currency   = COALESCE(dp.canonical_currency, 'EUR')
   AND CAST(raw.date AS DATE) BETWEEN fx.valid_from AND fx.valid_to
