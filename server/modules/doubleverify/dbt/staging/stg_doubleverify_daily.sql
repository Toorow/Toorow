-- Staging: DoubleVerify raw (long format) -> canonical grain.
-- AD-7: QUALIFY supersede -- latest pull per grain wins (ULIDs are lex-monotonic).
-- AD-4: only additive count metrics are stored raw; every *_rate is dropped by
--        transform() before landing and recomputed at the semantic layer.
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
FROM {{ source('raw_doubleverify', 'raw_doubleverify_daily') }}
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY project_id, date, metric, breakdown_dimension, breakdown_value
    ORDER BY pull_id DESC
) = 1
