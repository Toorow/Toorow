-- Staging: Pinterest Ads LONG-format daily landing (Story 26.3).
-- AD-7: QUALIFY supersede -- the latest pull per grain wins. ULIDs are
-- lexicographically monotonic, so ORDER BY pull_id DESC = newest first.
--
-- GRAIN: one row per (project_id, date, data_level, ad_account_id,
-- campaign_id, ad_group_id, ad_id, pin_id, product_group_id, segments_json,
-- metric). The landing is LONG (metric, value_num) because catalog_daily can
-- select ANY of the 626 async catalog columns -- wide columns cannot hold an
-- arbitrary selection. segments_json carries the selected GRAIN-BEARING non-key
-- dimensions (breakdowns that produce distinct provider rows) as canonical
-- sorted-key JSON so any catalog dimension participates in the supersede grain
-- without schema churn.
--
-- attributes_json (story 26.6 Part A): DESCRIPTIVE MUTABLE entity STATE/TYPE
-- attributes (campaign/ad_group/ad/pin_promotion statuses, objective/budget
-- types) -- values that CHANGE over time for a fixed grain. They are carried
-- HORS the partition below (latest-wins via ORDER BY pull_id DESC), NEVER in
-- the QUALIFY grain key. Without this, the 26.1 refetch ladder re-reading a
-- window with a changed status would produce a different segments_json for the
-- same (grain, date), the stale row would survive the supersede, and every
-- metric of the re-pulled window would DOUBLE. attributes_json out of the
-- partition => one row per grain survives; attributes = the latest known.
--
-- data_level (CAMPAIGN | AD_GROUP | AD | PRODUCT_GROUP | KEYWORD -- story 3.6
-- lesson): each report grain lands under its own marker and is part of the
-- grain key, so a campaign-grain pull and an ad-grain pull for the same
-- campaign_id coexist without double-counting inside one series.
--
-- Micros rule (story 26.3): *_IN_MICRO_DOLLAR amounts are divided by 1e6 in
-- the connector's transform() BEFORE landing (conversion set derived from the
-- catalog 'value/1e6' marker) -- value_num is ALWAYS in currency units for
-- monetary metrics.
--
-- AD-4: provider-computed ratio metrics selected via catalog_daily (CTR,
-- ECPC_*, ROAS, rates...) land RAW at day grain (non-additive) -- NEVER SUM
-- them downstream; recompute ratios at the semantic layer.
{{ config(materialized='view') }}

SELECT
    date,
    data_level,
    ad_account_id,
    campaign_id,
    campaign_name,
    ad_group_id,
    ad_id,
    pin_id,
    product_group_id,
    segments_json,
    attributes_json,
    metric,
    value_num,
    pull_id,
    loaded_at,
    project_id
FROM {{ source('raw_pinterest_ads', 'raw_pinterest_ads_daily') }}
-- attributes_json is DELIBERATELY absent from the PARTITION BY (story 26.6
-- Part A): it is a latest-wins entity attribute, not a grain key. ORDER BY
-- pull_id DESC makes the surviving row carry the newest attributes.
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY project_id, date, data_level, ad_account_id, campaign_id,
                 ad_group_id, ad_id, pin_id, product_group_id,
                 COALESCE(segments_json, ''), metric
    ORDER BY pull_id DESC
) = 1
