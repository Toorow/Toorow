-- Story 22.4 plan-only honesty (décision 7, AD-9): a plan-only line (offline: TV,
-- OOH -- no actuals source) is PRESENT with its budget + allocations, but carries NO
-- pacing: actual_to_date, consumed_pct, pace and extrapolated_spend MUST ALL be NULL
-- -- never a misleading 0 %. The line still appears (its budget/allocated_to_date are
-- non-NULL) so the plan-only budget is visible; only the pacing is withheld.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

-- 1. A plan-only line must never carry a non-NULL actual/pacing figure.
SELECT
    project_id, plan_id, line_key,
    actual_to_date, consumed_pct, pace, extrapolated_spend,
    'plan_only_has_pacing' AS violation
FROM {{ ref('plan_pacing_by_line') }}
WHERE is_plan_only
  AND (
        actual_to_date     IS NOT NULL
     OR consumed_pct       IS NOT NULL
     OR pace               IS NOT NULL
     OR extrapolated_spend IS NOT NULL
  )

UNION ALL

-- 2. A plan-only line must still be PRESENT with its budget (not dropped).
SELECT
    project_id, plan_id, line_key,
    budget, NULL AS consumed_pct, NULL AS pace, NULL AS extrapolated_spend,
    'plan_only_missing_budget' AS violation
FROM {{ ref('plan_pacing_by_line') }}
WHERE is_plan_only
  AND (budget IS NULL)

UNION ALL

-- 3. plan-only lines carry NO ventilated actual at the daily grain either
--    (they hold no mapping, so actual_amount is NULL on every day).
SELECT
    project_id, plan_id, line_key,
    NULL AS budget, NULL AS consumed_pct, NULL AS pace, NULL AS extrapolated_spend,
    'plan_only_daily_actual_not_null' AS violation
FROM {{ ref('plan_vs_actual_daily') }}
WHERE is_plan_only
  AND actual_amount IS NOT NULL
