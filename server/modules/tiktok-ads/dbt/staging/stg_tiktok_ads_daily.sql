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
--   - cost: the SAME source-currency amount, converted at read, never here.
--   NOTHING IS NORMALIZED HERE, and the two lines above that said otherwise were
--   read as fact for one epic. Story 39.10 moved the conversion to the READ
--   ([[fx-locus-read-not-staging]]): this model preserves the source-currency
--   amount and CARRIES the rate provenance beside it; `fx_convert_at_read` in the
--   mart is what converts, once, and it can be re-derived after a policy change.

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
    -- FX evidence, emitted by ONE macro since the Story 67.13 cutover: the
    -- governed POSED rate is asked first and the seed below is the fallback,
    -- and a refusal (an unanswerable condition, a tie) serves no rate at all.
    -- `fx_as_of_date` is still the day the rate was QUOTED or DECLARED, never
    -- the day its window opens -- story 58.7's repair, carried into both arms.
    {{ toorow_fx_evidence_columns() }}
    raw.impressions,
    raw.clicks,
    raw.conversions,
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
-- Story 13.2: FX conflict resolution override (AD-6). RETROACTIVE -- this mart
-- is rebuilt in full on every run, so a binding applies to every day in scope.
-- Wording A, decided 2026-08-22 in
-- docs/product-architecture/capabilities/currency-fx.md.
LEFT JOIN {{ toorow_source_or_empty('mirror', 'fx_source_currency_bindings', [
        ['project_id', 'string'],
        ['target_field', 'string'],
        ['source_module', 'string'],
        ['resolved_source_currency', 'string'],
    ]) }} fx_res
    ON fx_res.project_id   = raw.project_id
   AND fx_res.target_field = 'cost'
   AND fx_res.source_module = 'tiktok-ads'
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
LEFT JOIN {{ toorow_fx_posed_resolution('tiktok-ads') }} fxp
    ON  fxp.project_id     = raw.project_id
   AND fxp.base_currency  = COALESCE(fx_res.resolved_source_currency, raw.cost_source_currency)
   AND fxp.quote_currency = dp.canonical_currency
   AND CAST(raw.date AS DATE) >= fxp.seg_from
   AND CAST(raw.date AS DATE) <  fxp.seg_until
LEFT JOIN {{ ref('fx_rates') }} fx
    ON fx.from_currency = COALESCE(fx_res.resolved_source_currency, raw.cost_source_currency)
   AND fx.to_currency   = dp.canonical_currency
   AND CAST(raw.date AS DATE) BETWEEN fx.valid_from AND fx.valid_to
