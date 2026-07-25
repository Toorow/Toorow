{{ config(materialized='view') }}
SELECT * FROM {{ source('raw_brevo', 'raw_brevo_daily') }}
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY project_id, profile, account_id, source_id, date, channel,
    event_type, protected_identifier, metric
  ORDER BY pull_id DESC
) = 1
