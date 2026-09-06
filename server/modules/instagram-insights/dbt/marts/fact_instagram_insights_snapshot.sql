{{ config(materialized='view') }}

-- Dedicated non-additive projection. Account reach cannot be summed across
-- dates/accounts, and media shares/comments are lifetime snapshots.
SELECT
    project_id,
    date,
    report_profile,
    account_id,
    media_id,
    media_type,
    media_product_type,
    permalink,
    caption,
    published_at,
    metric,
    value,
    pull_id,
    loaded_at
FROM {{ ref('stg_instagram_insights_daily') }}
WHERE non_additive = TRUE
