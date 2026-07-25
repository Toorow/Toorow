-- Staging: maps raw Meta Ads source fields to canonical names
-- connector-requirements.md Meta Ads profile
-- AD-4: additive metrics only (cost, impressions, clicks, conversions)
-- AD-7: pull_id propagated from raw for provenance chain
-- Story 3.6: module-owned staging (AI-06 decision Option A -- external model-path).
--
-- GRAIN: one row per (project_id, date, data_level, campaign_id, adset_id, ad_id).
-- review-15-9 F-1: data_level (CAMPAIGN | ADSET | CREATIVE) records WHICH report grain
-- landed each row. It is part of the grain key AND the supersede partition so a
-- campaign-grain pull and an adset-grain pull for the same campaign_id coexist without
-- colliding; downstream, EACH mart breakdown series reads ONLY the rows of its own
-- data_level (the campaign_id series NEVER sums the campaign_id carried by adset/creative
-- -grain rows). Higher-grain profiles land rows whose adset_id / ad_id are NULL.
--
-- LEGACY NULL data_level (documented COALESCE choice, review-15-9 F-1): rows landed
-- before the data_level column existed (NULL after the additive ALTER) are treated as
-- CAMPAIGN grain via COALESCE(data_level, 'CAMPAIGN'). Justification: the historical
-- Meta pull was campaign-level ONLY (a single bare pull() at level=campaign) -- every
-- pre-migration row IS a campaign-grain row, so COALESCE to 'CAMPAIGN' is honest, not
-- an invention. This keeps the campaign_id series correct across the migration boundary.
--
-- Supersede semantics (AD-7): when several pulls cover the same grain, the LATEST pull
-- wins. ULIDs are lexicographically monotonic, so ORDER BY pull_id DESC = newest first.
--
-- Story 4.2 (AC3, AC6): Cost currency normalization + timezone policy.
--
-- Timezone: Meta Ads Insights API dates are already bucketed in the account's
-- reporting timezone (set in Business Manager). At day grain, we re-label the date
-- to the project timezone when they differ; intraday precision is not available.
-- Source date preserved as 'date_source'. No actual time shift is performed at day grain.
-- Intraday re-labeling (account tz → Europe/Paris) would only matter for spend
-- occurring between account-tz midnight boundaries. Since the Meta API already buckets
-- by its own timezone, re-labeling at day grain means accepting the source tz boundary.
-- We store this as a policy decision (HG-4: do NOT add convert_timezone() at day grain).
-- Story 6.x or a future Epic 4 story can revisit if hourly Meta data is added.
--
-- Currency normalization (AD-6, HG-2, HG-3):
--   - cost_source_value: raw spend in the account's billing currency (preserved for reconciliation).
--   - cost_source_currency: billing currency from raw column (default 'USD' for seed data).
--   - cost: spend normalized to project canonical_currency (EUR by default) via fx_rates seed.
--   Normalization happens ONCE, here in dbt staging. No downstream code converts (AD-6).
--   When from_currency = to_currency (e.g. EUR→EUR), rate = 1.0 — no conversion needed.

WITH raw AS (
    SELECT
        *,
        -- review-15-9 F-1: legacy rows (pre-migration) carry data_level NULL -> treat
        -- them as CAMPAIGN grain (the only grain the historical pull produced).
        COALESCE(data_level, 'CAMPAIGN') AS data_level_resolved
    FROM {{ source('raw_meta', 'raw_meta_ads_daily') }}
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY project_id, date, COALESCE(data_level, 'CAMPAIGN'),
                     campaign_id, adset_id, ad_id
        ORDER BY pull_id DESC
    ) = 1
)

SELECT
    -- Timezone policy (AC6): date_source = raw date; date = re-labeled project date.
    -- At day grain these are identical (no intraday conversion performed — HG-4).
    raw.date     AS date_source,
    raw.date     AS date,
    -- review-15-9 F-1: propagate the report grain so each mart series filters to its own.
    raw.data_level_resolved AS data_level,
    raw.campaign_id,
    raw.campaign_name,
    raw.adset_id,
    raw.adset_name,
    raw.ad_id,
    raw.creative_id,
    -- Cost normalization (Story 39.10 FX-at-read [[fx-locus-read-not-staging]]):
    -- Staging preserves immutable source-currency amount; conversion happens ONCE at read.
    raw.spend                                  AS cost_source_value,
    raw.cost_source_currency                   AS cost_source_currency,
    raw.spend                                  AS cost,
    -- FX provenance columns for read-time conversion
    fx.rate                                    AS fx_rate,
    fx.valid_from                              AS fx_as_of_date,
    'seed'                                     AS fx_source,
    'fixed'                                    AS fx_tier,
    raw.impressions,
    raw.clicks,
    raw.conversions,
    raw.pull_id,
    raw.loaded_at,
    raw.project_id
FROM raw
-- G-05: JOIN dim_project to obtain canonical_currency per project (AD-6/FR4).
-- COALESCE to 'EUR' when the project row is missing so seeds and tests still pass.
-- dim_project reads from source('mirror', 'project_preferences') only -- no cycle.
LEFT JOIN {{ ref('dim_project') }} dp
    ON dp.project_id = raw.project_id
-- Story 13.2: FX conflict resolution override (AD-6). When a resolution exists for
-- (project_id, 'cost', 'meta-ads'), the resolved_source_currency replaces the raw
-- column in the FX JOIN below. Non-retroactive: applies from the next dbt run.
LEFT JOIN {{ source('mirror', 'fx_conflict_resolutions') }} fx_res
    ON fx_res.project_id   = raw.project_id
   AND fx_res.target_field = 'cost'
   AND fx_res.source_module = 'meta-ads'
-- FX validity window: JOIN with raw.date BETWEEN valid_from AND valid_to.
-- raw.date is a VARCHAR ISO string (F-06 convention) -- cast for the DATE seed columns.
-- COALESCE(fx_res.resolved_source_currency, raw.cost_source_currency): prefer resolution.
LEFT JOIN {{ ref('fx_rates') }} fx
    ON fx.from_currency = COALESCE(fx_res.resolved_source_currency, raw.cost_source_currency)
   AND fx.to_currency   = COALESCE(dp.canonical_currency, 'EUR')
   AND CAST(raw.date AS DATE) BETWEEN fx.valid_from AND fx.valid_to
