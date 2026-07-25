-- Staging: The Trade Desk LONG-format daily landing (Story 28.2).
-- AD-7: QUALIFY supersede -- the latest pull per grain wins. ULIDs are
-- lexicographically monotonic, so ORDER BY pull_id DESC = newest first.
--
-- GRAIN: one row per (project_id, date, report_template, partner_id,
-- advertiser_id, campaign_id, ad_group_id, creative_id, segments_json,
-- metric). The landing is LONG (metric, value_num) because catalog_daily can
-- select ANY exposed column of the 263-column surface -- wide columns cannot
-- hold an arbitrary selection. segments_json carries the selected non-key
-- dimensions as canonical sorted-key JSON so any catalog column participates
-- in the supersede grain without schema churn.
--
-- report_template = the managed ReportTemplate that produced this row
-- (daily_performance | conversions | creative | geo_platform | video_player);
-- it is part of the grain key so coexisting report grains never double-count
-- inside one series. advertiser_id is the MyReports reporting anchor.
--
-- Attribution restatement (AD-7 + refetch ladder 3/14/45): TTD restates
-- conversion families (click/view-through/touch/time-weighted-decay, pixels
-- 01-06) as attribution windows close (last ~3-7 days provisional). Re-pulls
-- land as new pull_ids and this QUALIFY keeps the most recent value per grain
-- -- restatements are absorbed, never double-counted.
--
-- AD-4: provider-computed ratio columns (Cpm/Cpc/Ctr/Cpa*) selected via
-- catalog_daily land RAW at day grain (NON-ADDITIVE) -- NEVER SUM them
-- downstream; recompute ratios at the semantic layer from stored
-- numerators/denominators. PlayerRewind is an ADDITIVE count (not a ratio).
{{ config(materialized='view') }}

SELECT
    date,
    report_template,
    partner_id,
    advertiser_id,
    campaign_id,
    campaign_name,
    ad_group_id,
    ad_group_name,
    creative_id,
    segments_json,
    metric,
    value_num,
    pull_id,
    loaded_at,
    project_id
FROM {{ source('raw_thetradedesk', 'raw_thetradedesk_daily') }}
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY project_id, date, report_template, partner_id, advertiser_id,
                 campaign_id, ad_group_id, creative_id,
                 COALESCE(segments_json, ''), metric
    ORDER BY pull_id DESC
) = 1
