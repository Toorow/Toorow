-- Staging: Amazon Ads LONG-format daily landing (Story 26.5).
-- AD-7: QUALIFY supersede -- the latest pull per grain wins. ULIDs are
-- lexicographically monotonic, so ORDER BY pull_id DESC = newest first.
--
-- GRAIN: one row per (project_id, date, data_level, ad_product, region,
-- profile_id, campaign_id, ad_group_id, ad_id, keyword_id, search_term,
-- advertised_asin, purchased_asin, segments_json, metric).
-- The landing is LONG (metric, value_num) because catalog_daily can select
-- ANY exposed column of the 740-column catalog -- wide columns cannot hold an
-- arbitrary selection. segments_json carries the selected non-key dimensions
-- as canonical sorted-key JSON so any catalog column participates in the
-- supersede grain without schema churn.
--
-- Story 26.6 Part A (finding F-2): entity STATE/TYPE MUTABLE attributes
-- (campaignStatus, adStatus, adKeywordStatus, ... -- the connector's
-- descriptive_mutable_fields.json set) do NOT belong in segments_json: a status
-- flip (ENABLED -> PAUSED) on a re-pulled day would mint a SECOND grain key and
-- the stale row would SURVIVE this QUALIFY -> SUM(cost) would double for the
-- whole refetched window (the 26.1 ladder makes this systematic). They land in
-- a SEPARATE attributes_json column that is LATEST-WINS and is NEVER part of the
-- PARTITION BY below -- the newest pull's attributes ride along with the kept
-- row, without ever splitting the grain.
--
-- data_level = the v3 reportTypeId (spCampaigns | sbCampaigns | sdCampaigns |
-- spTargeting | ... -- story 3.6 lesson) and ad_product = the v3 adProduct
-- (SPONSORED_PRODUCTS | SPONSORED_BRANDS | SPONSORED_DISPLAY |
-- SPONSORED_TELEVISION): both are part of the grain key so coexisting report
-- grains and ad products never double-count inside one series. Each mart
-- breakdown series reads ONLY the rows of its own (data_level, ad_product).
--
-- region/profile_id: the 3 API regions are ISOLATED silos; region + profile
-- are part of the grain key (the same advertiser can hold profiles in
-- several marketplaces).
--
-- Attribution restatement (AD-7 + refetch ladder 3/14/45): Amazon restates
-- conversions 1/7/28 days after the conversion event on the AD-INTERACTION
-- date (up to 42 days of movement). Re-pulls land as new pull_ids and this
-- QUALIFY keeps the most recent value per grain -- restatements are absorbed,
-- never double-counted.
--
-- AD-4: provider-computed ratio columns selected via catalog_daily land RAW
-- at day grain (non-additive) -- NEVER SUM them downstream; recompute ratios
-- at the semantic layer from stored numerators/denominators.
--
-- AI-270 -- FX PROVENANCE, so this staging can reach `fact_daily_kpi`. Same shape
-- as google-ads and microsoft-ads: `money_evidence_present` needs fx_rate /
-- fx_as_of_date / fx_source / fx_tier in scope, joined here because the source
-- currency is a property of the landed row. No conversion at staging, no parity
-- fallback.
{{ config(materialized='view') }}

WITH raw AS (
    SELECT
        date,
        data_level,
        ad_product,
        region,
        profile_id,
        campaign_id,
        campaign_name,
        ad_group_id,
        ad_group_name,
        ad_id,
        keyword_id,
        search_term,
        advertised_asin,
        purchased_asin,
        segments_json,
        attributes_json,
        metric,
        value_num,
        cost_source_currency,
        pull_id,
        loaded_at,
        project_id
    FROM {{ source('raw_amazon_ads', 'raw_amazon_ads_daily') }}
    -- La supersede porte sur les lignes BRUTES : avant les jointures, sinon une
    -- ligne perdante survivrait en se distinguant par un taux.
    QUALIFY ROW_NUMBER() OVER (
        -- attributes_json is DELIBERATELY absent here (Story 26.6 Part A): mutable
        -- entity state/type attributes must not split the grain. The newest pull's
        -- attributes_json rides along with the surviving row via ORDER BY pull_id.
        PARTITION BY project_id, date, data_level, ad_product, region, profile_id,
                     campaign_id, ad_group_id, ad_id, keyword_id, search_term,
                     advertised_asin, purchased_asin,
                     COALESCE(segments_json, ''), metric
        ORDER BY pull_id DESC
    ) = 1
)

SELECT
    raw.date,
    raw.data_level,
    raw.ad_product,
    raw.region,
    raw.profile_id,
    raw.campaign_id,
    raw.campaign_name,
    raw.ad_group_id,
    raw.ad_group_name,
    raw.ad_id,
    raw.keyword_id,
    raw.search_term,
    raw.advertised_asin,
    raw.purchased_asin,
    raw.segments_json,
    raw.attributes_json,
    raw.metric,
    raw.value_num,
    raw.cost_source_currency,
    raw.value_num                              AS cost_source_value,
    {{ toorow_fx_evidence_columns() }}
    raw.pull_id,
    raw.loaded_at,
    raw.project_id
FROM raw
LEFT JOIN {{ ref('dim_project') }} dp
    ON dp.project_id = raw.project_id
LEFT JOIN {{ toorow_source_or_empty('mirror', 'fx_source_currency_bindings', [
        ['project_id', 'string'],
        ['target_field', 'string'],
        ['source_module', 'string'],
        ['resolved_source_currency', 'string'],
    ]) }} fx_res
    ON fx_res.project_id    = raw.project_id
   AND fx_res.target_field  = 'cost'
   AND fx_res.source_module = 'amazon-ads'
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
LEFT JOIN {{ toorow_fx_posed_resolution('amazon-ads') }} fxp
    ON  fxp.project_id     = raw.project_id
   AND fxp.base_currency  = COALESCE(fx_res.resolved_source_currency, raw.cost_source_currency)
   AND fxp.quote_currency = dp.canonical_currency
   AND CAST(raw.date AS DATE) >= fxp.seg_from
   AND CAST(raw.date AS DATE) <  fxp.seg_until
LEFT JOIN {{ ref('fx_rates') }} fx
    ON fx.from_currency = COALESCE(fx_res.resolved_source_currency, raw.cost_source_currency)
   AND fx.to_currency   = dp.canonical_currency
   AND CAST(raw.date AS DATE) BETWEEN fx.valid_from AND fx.valid_to
