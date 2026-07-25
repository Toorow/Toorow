-- Staging: raw_generic_daily -> canonical long-format KPI rows
-- Story 8.10: Generic datastream (module_kind='kpi').
-- R4: any day>measure tabular payload grafts on with zero core change.
-- R3: date normalization already applied at landing (connector.py _normalize_date).
--
-- Grain: (project_id, date, metric, breakdown_dimension, breakdown_value)
-- AD-7: QUALIFY supersede semantics -- latest pull_id wins per grain.
-- ULIDs are lexicographically monotonic, so ORDER BY pull_id DESC = newest first.
-- AD-4: value is a raw DOUBLE; aggregation semantics declared in target_fields.
{{ config(materialized='view') }}

SELECT
    project_id,
    date,
    metric,
    value,
    breakdown_dimension,
    breakdown_value,
    pull_id,
    loaded_at
FROM {{ source('raw_generic', 'raw_generic_daily') }}
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY project_id, date, metric, breakdown_dimension, breakdown_value
    ORDER BY pull_id DESC
) = 1
