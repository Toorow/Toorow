{{ config(materialized='view') }}
SELECT * FROM {{ source('raw_adobe_analytics', 'raw_adobe_analytics_daily') }}
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY project_id, report_profile, global_company_id, rsid, date,
    dimension, item_id, parent_item_id, segment_ids, metric
  ORDER BY pull_id DESC
) = 1
