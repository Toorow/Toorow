-- test_projection_min_breakdown_stable.sql -- Story 12.4 AC3.
--
-- The canonical single-dimension partition per (project_id, date, connector,
-- metric) is picked by MIN(breakdown_dimension). The 12.4 safe projection must
-- NOT change that choice: for GA4 the canonical MIN must stay 'country', so
-- metric_baselines / cross_source_conversions / compute_rollup keep summing a
-- single full-coverage series (no double count). This is the dbt counterpart of
-- rollup.py canonical_breakdown_per_connector's byte-identical pinning
-- (style of test_ga4_acquisition_min_breakdown_stable.sql).
--
-- 12.4 adds NO new breakdown_dimension to fact_daily_kpi (the full grain lands in
-- the SEPARATE candidate_full_grain relation, not as a fact partition). So MIN
-- must be byte-identical to before. Any governed dimension projection a future
-- compile adds must sort so MIN never selects it (a parallel series, never a new
-- canonical slot) -- this test is the standing guard that fires the moment a
-- projection wrongly squeezes a dimension into the canonical breakdown slot.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass). We return any
-- (project_id, date, metric) whose GA4 MIN(breakdown_dimension) is NOT 'country'
-- for the additive metrics that carry a 'country' partition.

SELECT
    project_id,
    date,
    metric,
    MIN(breakdown_dimension) AS min_dim
FROM {{ ref('fact_daily_kpi') }}
WHERE connector = 'google-analytics'
  AND metric IN ('sessions', 'active_users', 'conversions')
GROUP BY project_id, date, metric
HAVING MIN(breakdown_dimension) <> 'country'
