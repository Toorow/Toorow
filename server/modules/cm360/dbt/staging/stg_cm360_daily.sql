{{ config(materialized='view') }}

-- Full-grain supersede: every selected dimension remains in dimensions_json and
-- the common physical keys participate directly in the grain.
SELECT
    report_profile,
    profile_id,
    account_id,
    subaccount_id,
    advertiser_id,
    date,
    campaign_id,
    placement_id,
    creative_id,
    floodlight_activity_id,
    dimensions_json,
    metric,
    value,
    provider_value,
    non_additive,
    pull_id,
    loaded_at,
    project_id
FROM {{ source('raw_cm360', 'raw_cm360_daily') }}
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY project_id, report_profile, profile_id, advertiser_id, date,
                 COALESCE(campaign_id, ''), COALESCE(placement_id, ''),
                 COALESCE(creative_id, ''), COALESCE(floodlight_activity_id, ''),
                 dimensions_json, metric
    ORDER BY pull_id DESC
) = 1
