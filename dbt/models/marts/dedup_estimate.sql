-- dedup_estimate: estimated deduplication of régie-claimed conversions against a
-- project-designated source of truth (Epic 17, Story 17.2 / CAP-20 / FR23).
--
-- ============================ THIS MART IS A READ ============================
-- dedup_estimate READS fact_daily_kpi + dim_project only. It creates NO new fact
-- rows and edits NO existing model. fact_daily_kpi / cross_source_conversions /
-- metric_baselines are STRICTLY UNTOUCHED (Epic 17 invariant AD-4/AD-6; the
-- "totaux inchangés" discipline of review-epic-10). It GENERALISES the Story 3.7
-- dedup rule (cross_source_conversions picks ONE winning source per day; here we
-- estimate the WEIGHTED contribution of each claiming régie against the designated
-- truth) -- it does NOT duplicate or modify cross_source_conversions.
--
-- GRAIN (enforced by dedup_estimate_grain_unique):
--   one row per project_id x date x channel_connector,
--   where channel_connector is a régie that CLAIMS conversions that day.
-- The shared per-(project,date) aggregates (verified_total, claimed_conversions
-- summed over régies, duplication_rate) are repeated on each channel row so the
-- row is self-describing; deduplicated_contribution is the per-channel value.
--
-- ===================== FORMULAS (doc/synthese §2.2, AD-9) ====================
--   duplication_rate           = SUM(claimed over régies) / NULLIF(verified, 0)
--   deduplicated_contribution  = claimed_channel * verified / NULLIF(SUM(claimed), 0)
-- The deduplicated contributions across channels sum to verified_total (when
-- verified is non-NULL and SUM(claimed) > 0) -- each régie is weighted by its share
-- of the claimed volume and rescaled onto the real total. This is an ESTIMATION
-- (aggregate, not a user-level join): without the BigQuery user_pseudo_id export
-- (CAP-14/AD-14, Phase B) we cannot know WHICH claimed conversions overlap, only
-- estimate the aggregate overlap. estimate_label carries that honesty on every row.
--
-- ===================== SOURCE OF TRUTH (dim_project, 17.1) ===================
-- OPT-IN: a project with verification_source_type = NULL produces ZERO rows here
-- (dedup is opt-in per project). verified_total is read per type:
--   'ga4'     -> GA4 conversions on the canonical single breakdown dimension
--                (MIN(breakdown_dimension) per (project,date,connector) -- same
--                 canonical-dim pattern as cross_source_conversions; NEVER the sum
--                 of the parallel country/device/user_type/... partitions).
--   'shopify' -> Shopify orders_count on breakdown_dimension='day_total' (15.4).
--   'stripe'  -> NULL v1: Stripe exposes revenue (payments), NOT a conversion volume
--                comparable to regie-claimed conversions. There is also no reliable
--                GA4-joinable transaction key at scale (client_reference_id requires
--                Stripe Checkout and is not guaranteed -- Story 15.7). Reconciliation
--                with Stripe remains aggregate-only (revenue day totals via
--                cross_source_revenue). We emit NULL (a documented gap), NEVER an
--                invented number. duplication_rate is then NULL too.
--
-- HONESTY 1 (lead_event_name NOT filterable today -- BLOCKED 16.1 AC10 Phase B):
--   fact_daily_kpi does NOT carry GA4 event NAMES (it carries the 'conversions'
--   metric aggregated per breakdown, not per event). So for verification_source_type
--   ='ga4', verified_total is the connector's TOTAL conversions, NOT conversions
--   filtered to dim_project.lead_event_name. The per-event lead filter arrives with
--   Story 16.1 AC10 Phase B (event-scoped ingestion). Documented here, in schema.yml,
--   and in the story file. lead_event_name is read into the output for provenance so
--   the card/rollup can say WHICH event the project designated, but it does not (yet)
--   change the number.
--
-- HONESTY 2 (claimed = canonical partition, never the sum of parallel partitions):
--   a claimant régie may carry several parallel breakdown partitions that EACH total
--   the day (e.g. meta-ads: campaign_id / adset_id / ad_id). Summing them would
--   double-count (review-1-6 lesson). We take ONE canonical partition per claimant,
--   declared in the conversion_claimants seed (meta-ads -> 'ad_id'), mirroring the
--   canonical-dim discipline of cross_source_conversions.
--
-- HONESTY 3 (régies indisponibles -> rate NULL, never a disguised 0):
--   when the source of truth is configured but NO claimant régie has conversions
--   that day (SUM(claimed) = 0 or absent), there is no channel row to emit and no
--   duplication_rate to compute. duplication_rate is NULL whenever verified is
--   NULL/0 too (NULLIF guard) -- never a fabricated 0% duplication.
--
-- ===================== CLAIMANTS ARE DECLARATIVE (extensible) ================
-- The list of conversion-claiming régies is DECLARATIVE in the seed
-- dbt/seeds/conversion_claimants.csv (connector, canonical_breakdown_dimension) --
-- NOT hardcoded in this SQL. v1 lists meta-ads only (the ONLY connector that emits
-- 'conversions' as a régie today; GA4 'conversions' is the TRUTH source, not a
-- claimant, so it is deliberately absent from the seed). Adding TikTok / LinkedIn /
-- Google Ads as claimants is a ONE-LINE seed edit once those connectors land
-- 'conversions' in fact_daily_kpi -- zero change to this model.
--
-- AD-9 provenance: pull_id propagated (MAX per group) for the claimed side.
-- estimate_label is the constant 'estimation' on every row (the AD-9 semantics
-- carried by the data itself, never presented as an exact measure).

{{ config(materialized='view') }}

WITH prefs AS (
    -- OPT-IN gate: only projects that designated a source of truth participate.
    SELECT
        project_id,
        verification_source_type,
        verification_source_id,
        lead_event_name
    FROM {{ ref('dim_project') }}
    WHERE verification_source_type IS NOT NULL
),

conv AS (
    -- All conversion rows (per-source, provenance intact -- we READ, never rewrite).
    SELECT project_id, date, connector, breakdown_dimension, value, pull_id
    FROM {{ ref('fact_daily_kpi') }}
    WHERE metric = 'conversions'
),

-- ------------------------------------------------------------------ CLAIMED --
-- Per claimant régie: its day total on the ONE canonical breakdown dimension
-- declared in the seed (never the sum of its parallel partitions).
claimed AS (
    SELECT
        c.project_id,
        c.date,
        c.connector                       AS channel_connector,
        SUM(c.value)                      AS claimed_conversions,
        MAX(c.pull_id)                    AS pull_id
    FROM conv c
    JOIN {{ ref('conversion_claimants') }} cl
        ON cl.connector = c.connector
       AND cl.canonical_breakdown_dimension = c.breakdown_dimension
    -- restrict to opt-in projects only
    JOIN prefs p
        ON p.project_id = c.project_id
    GROUP BY c.project_id, c.date, c.connector
),

claimed_sum AS (
    SELECT project_id, date, SUM(claimed_conversions) AS claimed_total
    FROM claimed
    GROUP BY project_id, date
),

-- ------------------------------------------------------------------ VERIFIED --
-- GA4 truth: conversions on the canonical single dimension (MIN(breakdown_dimension)
-- per project/date/connector), NEVER the sum of parallel partitions. NB (HONESTY 1):
-- this is the connector TOTAL conversions, NOT filtered to lead_event_name (event
-- names are not in fact_daily_kpi; the per-event filter is BLOCKED on 16.1 AC10 Phase B).
ga4_canonical_dim AS (
    SELECT project_id, date, connector, MIN(breakdown_dimension) AS dim
    FROM conv
    WHERE connector = 'google-analytics'
    GROUP BY project_id, date, connector
),

verified_ga4 AS (
    SELECT c.project_id, c.date, SUM(c.value) AS verified_total
    FROM conv c
    JOIN ga4_canonical_dim d
        ON d.project_id = c.project_id AND d.date = c.date
       AND d.connector = c.connector AND d.dim = c.breakdown_dimension
    WHERE c.connector = 'google-analytics'
    GROUP BY c.project_id, c.date
),

-- Shopify truth: orders_count on the single 'day_total' partition (Story 15.4).
verified_shopify AS (
    SELECT project_id, date, SUM(value) AS verified_total
    FROM {{ ref('fact_daily_kpi') }}
    WHERE connector = 'shopify'
      AND metric = 'orders_count'
      AND breakdown_dimension = 'day_total'
    GROUP BY project_id, date
),

-- Resolve verified_total per opt-in project by its designated source type.
-- 'stripe' -> NULL v1: Stripe revenue != conversion volume; no reliable GA4 join key
--   at scale (client_reference_id not guaranteed -- Story 15.7). NULL propagates.
verified AS (
    SELECT
        p.project_id,
        p.verification_source_type,
        p.verification_source_id,
        p.lead_event_name,
        cs.date,
        CASE p.verification_source_type
            WHEN 'ga4'     THEN vg.verified_total
            WHEN 'shopify' THEN vs.verified_total
            ELSE NULL   -- 'stripe': revenue != volume de conversions; pas de clé GA4
                        -- fiable (15.7). Et tout type futur non câblé.
        END AS verified_total
    FROM prefs p
    -- drive the date axis off the days that actually have claimed régie volume
    JOIN claimed_sum cs
        ON cs.project_id = p.project_id
    LEFT JOIN verified_ga4 vg
        ON vg.project_id = p.project_id AND vg.date = cs.date
    LEFT JOIN verified_shopify vs
        ON vs.project_id = p.project_id AND vs.date = cs.date
)

SELECT
    cl.project_id,
    cl.date,
    cl.channel_connector,
    -- per-channel claimed (canonical partition of this régie)
    cl.claimed_conversions,
    -- shared aggregates (repeated per channel row so each row is self-describing)
    v.verified_total,
    cs.claimed_total,
    v.verification_source_type,
    v.verification_source_id,
    v.lead_event_name,
    -- duplication_rate = SUM(claimed) / verified. NULL when verified NULL/0 (HONESTY 3):
    -- never a disguised 0% duplication.
    cs.claimed_total / NULLIF(v.verified_total, 0) AS duplication_rate,
    -- deduplicated_contribution = claimed_channel * verified / SUM(claimed).
    -- NULL when verified NULL (stripe/absent) or claimed_total 0 (NULLIF guard).
    v.verified_total * cl.claimed_conversions
        / NULLIF(cs.claimed_total, 0) AS deduplicated_contribution,
    -- AD-9: this is an aggregate ESTIMATION, never an exact measure. Constant label.
    'estimation'                                   AS estimate_label,
    cl.pull_id
FROM claimed cl
JOIN claimed_sum cs
    ON cs.project_id = cl.project_id AND cs.date = cl.date
JOIN verified v
    ON v.project_id = cl.project_id AND v.date = cl.date
