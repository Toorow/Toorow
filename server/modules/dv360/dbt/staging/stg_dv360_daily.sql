{{ config(materialized='view') }}

SELECT
    report_profile,
    partner_id,
    advertiser_id,
    date,
    campaign_id,
    insertion_order_id,
    line_item_id,
    creative_id,
    conversion_type,
    country,
    youtube_ad_group_id,
    timezone,
    currency,
    dimensions_json,
    metric,
    value,
    provider_value,
    non_additive,
    pull_id,
    loaded_at,
    project_id
FROM {{ source('raw_dv360', 'raw_dv360_daily') }}
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY project_id, report_profile, partner_id, advertiser_id, date,
                 COALESCE(campaign_id, ''), COALESCE(insertion_order_id, ''),
                 COALESCE(line_item_id, ''), COALESCE(creative_id, ''),
                 COALESCE(conversion_type, ''), COALESCE(country, ''),
                 COALESCE(youtube_ad_group_id, ''),
                 COALESCE(timezone, ''), COALESCE(currency, ''), dimensions_json, metric
    ORDER BY pull_id DESC
) = 1
