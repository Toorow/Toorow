{{ config(materialized='view') }}

-- Latest pull wins at the complete raw metric grain. Media metrics are lifetime
-- snapshots and remain explicitly non-additive; only account profile_views can
-- flow into the additive cross-source mart.
SELECT
    report_profile,
    date,
    account_id,
    media_id,
    media_type,
    media_product_type,
    permalink,
    caption,
    published_at,
    metric,
    value,
    non_additive,
    payload_json,
    report_timezone,
    pull_id,
    loaded_at,
    project_id
FROM {{ source('raw_instagram_insights', 'raw_instagram_insights_daily') }}
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY project_id, report_profile, date, account_id,
                 COALESCE(media_id, ''), metric
    ORDER BY pull_id DESC
) = 1
