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
{{ config(materialized='view') }}

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
    attributes_json,   -- Story 26.6 Part A: latest-wins, NEVER in the partition
    metric,
    value_num,
    cost_source_currency,
    pull_id,
    loaded_at,
    project_id
FROM {{ source('raw_amazon_ads', 'raw_amazon_ads_daily') }}
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
