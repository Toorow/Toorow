-- Staging: Microsoft Ads LONG-format daily landing (Story 26.4).
-- AD-7: QUALIFY supersede -- the latest pull per grain wins. ULIDs are
-- lexicographically monotonic, so ORDER BY pull_id DESC = newest first.
--
-- GRAIN: one row per (project_id, date, data_level, account_id, campaign_id,
-- ad_group_id, ad_id, keyword_id, search_query, segments_json, metric).
-- The landing is LONG (metric, value_num) because catalog_daily can select
-- ANY of the 192 cataloged columns across the 8 covered report types -- wide
-- columns cannot hold an arbitrary selection. segments_json carries the
-- selected non-key SEGMENTING dimensions (age/gender, device, geo, ...) as
-- canonical sorted-key JSON so any catalog dimension participates in the
-- supersede grain without schema churn.
--
-- Story 26.6 Part A: attributes_json carries the DESCRIPTIVE-MUTABLE entity
-- status/type attributes (campaign_status, ad_status, campaign_type, ...).
-- These MUTATE across the refetch ladder re-pulls (a campaign Active on a day
-- can be re-pulled as Paused for the SAME day), so they are DELIBERATELY kept
-- OUT of the QUALIFY partition below (which is the supersede grain). If they
-- were in the partition, a status change would fork the grain and let the
-- stale row survive -> SUM(cost) would double for the whole re-pulled window
-- (finding F-1). attributes_json is latest-wins: it rides the surviving row
-- (ORDER BY pull_id DESC) so downstream reads the LAST KNOWN status/type.
--
-- data_level (CAMPAIGN | AD_GROUP | AD | KEYWORD | SEARCH_QUERY | ACCOUNT |
-- GEOGRAPHIC | AGE_GENDER -- one marker per whitelisted report type, story
-- 3.6 lesson): each report grain lands under its own marker and is part of
-- the grain key, so coexisting grains never double-count inside one series.
--
-- Restatement (story 26.4): Microsoft credits conversions/revenue to the
-- CLICK date with goal windows up to 90 days + invalid-traffic adjustments
-- (~1 week). The refetch ladder (3/30/90) re-pulls past days; AD-7
-- append-only + this QUALIFY makes every re-pull safe.
--
-- AD-4: ratio statistics selected via catalog_daily land RAW at day grain
-- (non-additive) -- NEVER SUM them downstream; recompute ratios at the
-- semantic layer from stored numerators/denominators.
{{ config(materialized='view') }}

SELECT
    date,
    data_level,
    account_id,
    campaign_id,
    campaign_name,
    ad_group_id,
    ad_group_name,
    ad_id,
    keyword_id,
    keyword,
    search_query,
    segments_json,
    attributes_json,
    metric,
    value_num,
    cost_source_currency,
    pull_id,
    loaded_at,
    project_id
FROM {{ source('raw_microsoft_ads', 'raw_microsoft_ads_daily') }}
-- Story 26.6 Part A: attributes_json is INTENTIONALLY ABSENT from this
-- partition -- it is a latest-wins descriptive column, never part of the grain.
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY project_id, date, data_level, account_id, campaign_id,
                 ad_group_id, ad_id, keyword_id, search_query,
                 COALESCE(segments_json, ''), metric
    ORDER BY pull_id DESC
) = 1
