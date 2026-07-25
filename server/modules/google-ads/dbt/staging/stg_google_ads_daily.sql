-- Staging: Google Ads LONG-format daily landing (Story 26.2).
-- AD-7: QUALIFY supersede -- the latest pull per grain wins. ULIDs are
-- lexicographically monotonic, so ORDER BY pull_id DESC = newest first.
--
-- GRAIN: one row per (project_id, date, data_level, customer_id, campaign_id,
-- ad_group_id, ad_id, criterion_id, search_term, segments_json, metric).
-- The landing is LONG (metric, value_num) because the catalog_daily profile can
-- select ANY of the 278 cataloged metrics -- wide columns cannot hold an
-- arbitrary selection. segments_json carries the selected ROW-BEARING
-- dimensions (GAQL segments: device, geo, ...) as canonical sorted-key JSON so
-- any catalog segment participates in the supersede grain without schema churn.
--
-- Story 26.6 Part A -- attributes_json vs the grain: DESCRIPTIVE-MUTABLE
-- attributes (status/type enums: campaign_status, ad_group_status,
-- ad_group_ad_status, campaign_advertising_channel_type, ... -- declared
-- descriptive_mutable in the catalog) land in a SEPARATE attributes_json column
-- and are DELIBERATELY EXCLUDED from the QUALIFY partition below. A campaign
-- ENABLED on the 1st then PAUSED on the 6th, re-pulled for the 1st by the 26.1
-- refetch ladder, comes back status=PAUSED; if the status were in segments_json
-- (a grain key) the changed value would fork the grain, the stale ENABLED row
-- would survive the QUALIFY and SUM(cost) would double for the whole re-pulled
-- window. Kept out of the grain, the re-pull keeps ONE row per grain, metrics
-- are never doubled, and attributes_json shows the LATEST-WINS status (the row
-- from the newest pull_id the QUALIFY selects). attributes_json NEVER enters
-- the partition.
--
-- data_level (CAMPAIGN | AD_GROUP | AD | KEYWORD | SEARCH_TERM -- story 3.6
-- lesson): each report grain lands under its own marker and is part of the
-- grain key, so a campaign-grain pull and an ad-grain pull for the same
-- campaign_id coexist without double-counting inside one series.
--
-- Micros rule (story 26.2): *_micros amounts are divided by 1e6 in the
-- connector's transform() BEFORE landing -- value_num is ALWAYS in currency
-- units for monetary metrics (cost_source_currency preserves the account
-- billing currency when customer_currency_code was selected).
--
-- AD-4: provider-computed ratio metrics selected via catalog_daily land RAW at
-- day grain (non-additive) -- NEVER SUM them downstream; recompute ratios at
-- the semantic layer from stored numerators/denominators.
{{ config(materialized='view') }}

SELECT
    date,
    data_level,
    customer_id,
    campaign_id,
    campaign_name,
    ad_group_id,
    ad_group_name,
    ad_id,
    criterion_id,
    keyword_text,
    search_term,
    segments_json,
    attributes_json,
    metric,
    value_num,
    cost_source_currency,
    pull_id,
    loaded_at,
    project_id
FROM {{ source('raw_google_ads', 'raw_google_ads_daily') }}
-- attributes_json is INTENTIONALLY ABSENT from this partition (story 26.6 Part
-- A): the supersede grain is (grain, date, data_level, segments_json) only, so
-- a mutable status change never forks the grain. The newest pull_id's row wins,
-- carrying its latest-wins attributes_json.
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY project_id, date, data_level, customer_id, campaign_id,
                 ad_group_id, ad_id, criterion_id, search_term,
                 COALESCE(segments_json, ''), metric
    ORDER BY pull_id DESC
) = 1
