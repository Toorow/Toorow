-- Story 8.11 (HARD REQUIREMENT (c), AD-4):
-- Non-additive metrics must NEVER be emitted as composite sub-dimension splits.
-- A composite row (breakdown_dimension containing '>') for a non-additive metric
-- would be a silently mis-aggregated total (e.g. summing average_position across
-- country x device is meaningless without impression weighting).
--
-- v1 excludes all non-additive metrics from composites; the impression-weighted
-- composite semantic view is deferred. This guard makes the exclusion enforceable
-- rather than incidental: any composite row for a known non-additive metric fails.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

SELECT
    project_id,
    date,
    connector,
    metric,
    breakdown_dimension,
    breakdown_value
FROM {{ ref('fact_daily_kpi') }}
WHERE breakdown_dimension LIKE '%>%'
  AND metric IN ('average_position', 'ctr', 'cpa', 'roas', 'cvr')
