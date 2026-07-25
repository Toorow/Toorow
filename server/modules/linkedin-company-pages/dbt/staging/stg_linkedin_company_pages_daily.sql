{{ config(materialized='view') }}
SELECT * FROM {{ source('raw_linkedin_company_pages', 'raw_linkedin_company_pages_daily') }}
QUALIFY ROW_NUMBER() OVER (PARTITION BY project_id, profile, organization_urn,
 entity_id, interval_start, interval_end, facet, facet_value, metric ORDER BY pull_id DESC)=1
