-- Staging: maps raw TikTok Ads source fields to canonical names.
-- connector-requirements.md TikTok Ads profile (Story 15.2, Epic 15).
-- AD-4: additive metrics only (cost, impressions, clicks, conversions).
-- AD-7: pull_id propagated from raw for provenance chain.
-- Story 15.2: module-owned staging (AI-06 decision Option A -- external model-path).
--
-- GRAIN: one row per (project_id, date, data_level, campaign_id, adgroup_id, ad_id).
-- review-15-2 F-1: data_level (AUCTION_CAMPAIGN | AUCTION_ADGROUP | AUCTION_AD) records
-- WHICH report grain landed each row. It is part of the grain key AND supersede partition
-- so a campaign-grain pull and an adgroup-grain pull for the same campaign_id coexist
-- without colliding; downstream, EACH mart breakdown series reads ONLY the rows of its
-- own data_level (the campaign_id series NEVER sums the campaign_id carried by adgroup-
-- grain rows). Higher-grain profiles land rows whose adgroup_id / ad_id are NULL.
-- tiktok-ads carries NEITHER country NOR device on any row (the BASIC report pull in this
-- project is at the campaign hierarchy grain) -- so, like meta-ads, NO country>device
-- composite is emitted downstream (AI-51: do not invent data).
--
-- Supersede semantics (AD-7): when several pulls cover the same grain, the LATEST pull
-- wins. ULIDs are lexicographically monotonic, so ORDER BY pull_id DESC = newest first.
--
-- CONVERSIONS (AD-4): conversions TikTok are CLAIMED by the channel (attributed by the
-- account window, default 7D click / 1D view -- declared in manifest.attribution_window).
-- They are additive at day grain but are NEVER summed with GA4/Meta conversions without
-- the cross-source dedup rule (Rule P, 3.7); the mart keeps them as distinct
-- connector='tiktok-ads' rows (same discipline as stg_meta_ads_daily).
--
-- Currency normalization (AD-6, pattern 4.2, mirrors stg_meta_ads_daily cost):
--   - cost_source_value: raw spend in the advertiser currency (preserved for reconciliation).
--   - cost_source_currency: advertiser currency from raw column (default 'EUR' for seed).
--   - cost: spend normalized to project canonical_currency (EUR by default) via fx_rates.
--   Normalization happens ONCE, here in dbt staging. When from = to (EUR->EUR), rate 1.0.

WITH raw AS (
    SELECT *
    FROM {{ source('raw_tiktok', 'raw_tiktok_ads_daily') }}
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY project_id, date, data_level, campaign_id, adgroup_id, ad_id
        ORDER BY pull_id DESC
    ) = 1
)

SELECT
    -- Timezone policy: date is the stat_time_day (source tz boundary accepted at day
    -- grain -- no intraday shift; same policy as stg_meta_ads_daily HG-4).
    raw.date     AS date_source,
    raw.date     AS date,
    -- review-15-2 F-1: propagate the report grain so each mart series filters to its own.
    raw.data_level,
    raw.campaign_id,
    raw.campaign_name,
    raw.adgroup_id,
    raw.adgroup_name,
    raw.ad_id,
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
-- dim_project supplies canonical_currency per project (AD-6/FR4).
-- COALESCE to 'EUR' when the project row is missing so seeds and tests still pass.
LEFT JOIN {{ ref('dim_project') }} dp
    ON dp.project_id = raw.project_id
-- Story 13.2: FX conflict resolution override (AD-6). Non-retroactive.
LEFT JOIN {{ source('mirror', 'fx_conflict_resolutions') }} fx_res
    ON fx_res.project_id   = raw.project_id
   AND fx_res.target_field = 'cost'
   AND fx_res.source_module = 'tiktok-ads'
-- FX validity window: raw.date is a VARCHAR ISO string (F-06) -- cast for the DATE seed columns.
LEFT JOIN {{ ref('fx_rates') }} fx
    ON fx.from_currency = COALESCE(fx_res.resolved_source_currency, raw.cost_source_currency)
   AND fx.to_currency   = COALESCE(dp.canonical_currency, 'EUR')
   AND CAST(raw.date AS DATE) BETWEEN fx.valid_from AND fx.valid_to
