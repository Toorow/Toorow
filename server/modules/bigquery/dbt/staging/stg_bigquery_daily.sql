-- Staging: replicated BigQuery table -> canonical names
-- AD-7: QUALIFY supersede -- latest pull per grain wins (ULIDs are lex-monotonic).
-- AD-4: values are stored raw with their breakdown; nothing is aggregated here.
--
-- The grain is (project, date, metric, breakdown) because a replicated table is
-- folded into long format at transform(): one row per numeric column per breakdown
-- value per day. Two pulls of the same window supersede rather than double-count.
{{ config(materialized='view') }}

SELECT
    date,
    metric,
    value,
    breakdown_dimension,
    breakdown_value,
    pull_id,
    loaded_at,
    project_id
FROM {{ source('raw_bigquery', 'raw_bigquery_daily') }}
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY project_id, date, metric, breakdown_dimension, breakdown_value
    ORDER BY pull_id DESC
) = 1
