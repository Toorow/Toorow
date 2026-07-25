{{ config(materialized='view') }}
SELECT * FROM {{ source('raw_sa360', 'raw_sa360_daily') }}
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY project_id, report_profile, manager_customer_id, customer_id,
               resource_id, date, COALESCE(campaign_id, ''), COALESCE(ad_group_id, ''),
               COALESCE(criterion_id, ''), COALESCE(device, ''),
               COALESCE(currency_code, ''), dimensions_json, metric
  ORDER BY pull_id DESC
) = 1
