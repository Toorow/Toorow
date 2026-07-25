-- test_full_grain_isolated_from_fact_kpi.sql -- Story 12.4 AC1/AC3.
--
-- The full-grain candidate relation (candidate_full_grain) is a DISTINCT artifact
-- from fact_daily_kpi: materializing it must write NO row to fact_daily_kpi and
-- change NO existing connector total. This proves the "two relations, one truth"
-- boundary at the mart grain -- the full grain is for analysis; it never becomes
-- the canonical fact (style of test_tiktok_totals_isolated.sql).
--
-- We prove isolation by RECONCILIATION, not by inspecting write logs (dbt models
-- are pure SELECTs). Both relations derive from the SAME stg_ga4_standard_daily
-- rows, so the full grain's own SUM of an additive metric per (project, date) MUST
-- equal fact_daily_kpi's canonical single-dimension ('country') total for GA4 --
-- if the full-grain relation had leaked an extra row into the fact or double-
-- counted, the two sides would diverge. Any (project, date, metric) where they
-- disagree beyond a rounding epsilon is returned (a returned row = FAIL).
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass). Zero seed
-- rows also pass (no candidate materialized yet), which is correct: the isolation
-- claim is vacuously true when the relation is empty.

WITH full_grain_totals AS (
    -- SUM over the ENTIRE joint grain collapses device_category x country back to
    -- the (project, date) day total for each additive metric.
    SELECT
        project_id,
        date,
        'sessions'  AS metric,
        SUM(sessions) AS full_grain_total
    FROM {{ ref('candidate_full_grain') }}
    GROUP BY project_id, date
    UNION ALL
    SELECT project_id, date, 'active_users', SUM(active_users)
    FROM {{ ref('candidate_full_grain') }}
    GROUP BY project_id, date
    UNION ALL
    SELECT project_id, date, 'conversions', SUM(conversions)
    FROM {{ ref('candidate_full_grain') }}
    GROUP BY project_id, date
),

-- fact_daily_kpi canonical single-dimension ('country') total for GA4 -- the same
-- canonical partition MIN(breakdown_dimension) pins (never the sum of parallel
-- breakdowns, which would double-count).
fact_country_totals AS (
    SELECT
        project_id,
        date,
        metric,
        SUM(value) AS fact_total
    FROM {{ ref('fact_daily_kpi') }}
    WHERE connector = 'google-analytics'
      AND breakdown_dimension = 'country'
      AND metric IN ('sessions', 'active_users', 'conversions')
    GROUP BY project_id, date, metric
)

SELECT
    f.project_id,
    f.date,
    f.metric,
    f.full_grain_total,
    k.fact_total
FROM full_grain_totals f
JOIN fact_country_totals k
    ON k.project_id = f.project_id
   AND k.date = f.date
   AND k.metric = f.metric
-- The full grain and the canonical fact derive from one truth: their day totals
-- must match. A 0.01 epsilon absorbs DOUBLE rounding only.
WHERE ABS(f.full_grain_total - k.fact_total) > 0.01
