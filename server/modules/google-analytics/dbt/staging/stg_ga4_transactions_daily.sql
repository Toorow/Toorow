-- Staging: GA4 transactions_daily profile (Epic 17, story 17.4).
-- Source: raw_ga4_transactions_daily (GA4 transactionId e-commerce dimension).
-- Carries (project_id, date, transaction_id, conversions, purchase_revenue) -- no
-- country/device to normalize, so NO normalize_dimension macros apply here.
-- AD-4: additive metrics only (conversions, purchase_revenue).
-- AD-7: pull_id propagated from raw for the provenance chain.
--
-- ========================= GRAIN DISCIPLINE (RULE ABSOLUE) =========================
-- GRAIN: one row per (project_id, date, transaction_id). transaction_id is the JOIN KEY
-- for the Shopify x GA4 reconciliation (doc/synthese §3.2), NOT a breakdown partition.
-- This model feeds ONLY the dedicated mart transaction_reconciliation.sql -- the
-- transaction grain NEVER enters fact_daily_kpi (lesson epic-10 + decision 15.4: a
-- per-transaction row does not total the connector day, so it is not a partition of a
-- breakdown; it is a join key like order_id on the Shopify side). There is deliberately
-- NO fact_daily_kpi UNION block for this staging model. This is the central discipline
-- point of story 17.4.
--
-- Supersede semantics (AD-7): when several pulls cover the same
-- (project_id, date, transaction_id), the LATEST pull wins.
-- ULIDs are lexicographically monotonic, so ORDER BY pull_id DESC = newest first.
--
-- transaction_id NULLABILITY (mirrors the Shopify F-7 honesty): a GA4 transaction may
-- carry no id ('(not set)' mapped to NULL in the connector before insert). Such a row
-- CANNOT be reconciled -- the mart filters transaction_id IS NULL out of the join
-- denominator (never a fake match bucket). The QUALIFY partitions on transaction_id, so
-- multiple NULL-id rows on the same day are kept distinct only if the raw layer produced
-- them per pull; in practice GA4 emits one row per transactionId value, and the id is the
-- grain. NULL-id rows are a documented, honestly-measured blind spot.
--
-- AI-53: transactionId dimension + conversions / purchaseRevenue metrics DECLARED per the
-- GA4 Data API doc (e-commerce) but NOT verified live (Phase B gate). Parallel,
-- non-interfering with every other GA4 staging model.

SELECT
    date,
    transaction_id,
    -- Additive metrics -- stored as plain values (AD-4).
    conversions,
    purchase_revenue,
    pull_id,
    loaded_at,
    -- project_id propagated from raw (seeded rows -> 'default'; real pulls carry own id).
    project_id
FROM {{ source('raw_ga4', 'raw_ga4_transactions_daily') }}
-- AD-7 supersede semantics: latest pull per (project_id, date, transaction_id).
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY project_id, date, transaction_id
    ORDER BY pull_id DESC
) = 1
