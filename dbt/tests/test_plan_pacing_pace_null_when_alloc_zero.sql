-- Story 22.4 honest-NULL guard (AD-9, décision 6): pace MUST be NULL whenever the
-- allocated-to-date is 0 (or NULL) -- NEVER a disguised 0 and NEVER a masked division
-- by zero. "Un jour sans allocation donne Pace NULL, jamais 0." Symmetrically,
-- consumed_pct must be NULL when budget is 0/NULL, and extrapolated_spend NULL when
-- days_elapsed is 0.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

-- pace must be NULL when allocated_to_date is 0 or NULL.
SELECT
    project_id, plan_id, line_key,
    allocated_to_date, actual_to_date, pace,
    'pace_not_null_on_zero_alloc' AS violation
FROM {{ ref('plan_pacing_by_line') }}
WHERE (allocated_to_date = 0 OR allocated_to_date IS NULL)
  AND pace IS NOT NULL

UNION ALL

-- consumed_pct must be NULL when budget is 0 or NULL.
SELECT
    project_id, plan_id, line_key,
    allocated_to_date, actual_to_date, consumed_pct,
    'consumed_pct_not_null_on_zero_budget' AS violation
FROM {{ ref('plan_pacing_by_line') }}
WHERE (budget = 0 OR budget IS NULL)
  AND consumed_pct IS NOT NULL

UNION ALL

-- extrapolated_spend must be NULL when days_elapsed is 0.
SELECT
    project_id, plan_id, line_key,
    allocated_to_date, actual_to_date, extrapolated_spend,
    'extrapolated_not_null_on_zero_elapsed' AS violation
FROM {{ ref('plan_pacing_by_line') }}
WHERE days_elapsed = 0
  AND extrapolated_spend IS NOT NULL
