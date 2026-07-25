{{ config(materialized='view') }}
SELECT * FROM {{ source('raw_taboola', 'raw_taboola_daily') }}
QUALIFY ROW_NUMBER() OVER (PARTITION BY project_id,account_id,report,dimension,date,
 campaign_id,item_id,site_id,breakdown_json,metric ORDER BY pull_id DESC)=1
