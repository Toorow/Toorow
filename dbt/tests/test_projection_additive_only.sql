-- test_projection_additive_only.sql -- Story 12.4 AC2/AC3.
--
-- The safe projection stores ONLY additive measures in fact_daily_kpi. Any
-- non-additive measure (ratios ctr/cpa/roas, average_position) is REJECTED from
-- additive projection by the compile gate (non_additive_measure_projected) and
-- routed to the semantic_* VIEW pattern -- NEVER emitted as a stored additive
-- fact row. This is the mart-side hard guard that a projection can never leak a
-- non-additive metric into fact_daily_kpi (reuse of the fact_kpi_metric_additive_only
-- / metric_not_ratio discipline, made explicit for the 12.4 projection surface).
--
-- fact_daily_kpi.value is an ADDITIVE value by contract (AD-4); a ratio /
-- average_position appearing as a stored metric would double-count or mis-average
-- at every downstream SUM. This test returns any fact_daily_kpi row whose metric
-- is a known non-additive measure -- a returned row = FAIL.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass). It aligns
-- with rollup.py._NON_ADDITIVE_METRICS (average_position, roas, ctr, cpa): those
-- names must never appear as stored fact metrics.

SELECT
    project_id,
    date,
    connector,
    metric,
    breakdown_dimension,
    breakdown_value,
    value
FROM {{ ref('fact_daily_kpi') }}
-- F-GRAIN-2 fix (Story 33-3): unique_reach and average_frequency are declared
-- non_additive=TRUE at CM360 landing (raw_cm360_daily.non_additive) and must never
-- be projected as stored additive facts. Added to the guard list so a future
-- developer cannot silently project reach metrics into fact_daily_kpi.
WHERE metric IN ('average_position', 'roas', 'ctr', 'cpa', 'cvr',
                 'unique_reach', 'average_frequency')
