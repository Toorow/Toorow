{{ config(materialized='view') }}
SELECT * FROM {{ source('raw_x_ads', 'raw_x_ads_daily') }}
QUALIFY ROW_NUMBER() OVER (PARTITION BY project_id, report_profile, account_id,
 entity_type, entity_id, placement, segment_type, segment_value, interval_start,
 interval_end, metric ORDER BY pull_id DESC) = 1
