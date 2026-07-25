-- Staging: raw IAS measurement -> canonical field names (already canonical at
-- ingest; connector.transform renames the camelCase API tokens before landing).
-- AD-4: all landed metrics are ADDITIVE counts (measured/viewable/eligible
--       impressions, invalid-traffic ads, brand-safety passed/failed ads). IAS
--       rates are NOT stored -- they are recomputed downstream from these counts.
-- AD-7: pull_id propagated from raw; QUALIFY supersede -- latest pull per grain wins
--       (ULIDs are lexicographically monotonic, so ORDER BY pull_id DESC = newest).
-- Grain: project_id x date x campaign_id (one row per campaign per day). The
--        raw table is shared by all IAS profiles; report_profile is carried for
--        provenance but is NOT part of the grain (every profile lands the same
--        campaign/day cell, filling the metric columns it measured).
{{ config(materialized='view') }}

SELECT
    date,
    campaign_id,
    campaign_name,
    measured_impressions,
    viewable_impressions,
    eligible_impressions,
    invalid_traffic_ads,
    brand_safety_passed_ads,
    brand_safety_failed_ads,
    report_profile,
    pull_id,
    loaded_at,
    project_id
FROM {{ source('raw_ias', 'raw_ias_daily') }}
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY project_id, date, campaign_id
    ORDER BY pull_id DESC
) = 1
