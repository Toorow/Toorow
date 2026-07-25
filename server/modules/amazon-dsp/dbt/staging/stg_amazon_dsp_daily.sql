{{ config(materialized='view') }}
SELECT * FROM {{ source('raw_amazon_dsp', 'raw_amazon_dsp_daily') }}
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY project_id, region, ads_account_id, advertiser_id, report_type,
    group_by, date, dimensions_json, metric
  ORDER BY pull_id DESC
) = 1
