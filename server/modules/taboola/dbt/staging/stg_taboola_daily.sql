{{ config(materialized='view') }}

WITH source AS (
    SELECT *, json_extract_string(breakdown_json, '$.country') AS country_source
    FROM {{ source('raw_taboola', 'raw_taboola_daily') }}
)

SELECT
    account_id,
    report,
    dimension,
    date,
    campaign_id,
    item_id,
    site_id,
    breakdown_json,
    country_source,
    {{ normalize_dimension('upper(country_source)', ref('dim_country'), 'aliases', 'iso_code') }} AS country,
    metric,
    value,
    non_additive,
    timezone,
    currency,
    update_time,
    pull_id,
    loaded_at,
    project_id
FROM source
QUALIFY ROW_NUMBER() OVER (PARTITION BY project_id,account_id,report,dimension,date,
 campaign_id,item_id,site_id,breakdown_json,metric ORDER BY pull_id DESC)=1
