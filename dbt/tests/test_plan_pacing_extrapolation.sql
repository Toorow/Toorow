-- Story 22.4 worked-example guard (the story's Given/When/Then chiffré, AD-9): for
-- the seeded line "line-digital-a" (budget 3 000 EUR over 30 days = 100 EUR/j, spend
-- 1 250 EUR over the first 10 days), the pacing figures MUST be:
--   consumed_pct       = 1250 / 3000        = 0.41666...   (41,7 %)
--   pace               = (1250 - 1000)/1000 = 0.25         (+25 %)
--   remaining_budget   = 3000 - 1250        = 1750
--   extrapolated_spend = 1250 / 10 * 30     = 3750
-- This pins the exact arithmetic of the formulas on a known case (the seed provides a
-- deterministic FIXED-DATE fixture; see dbt/seeds README / the Python mirror seeder).
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass). The row is only
-- checked WHEN the seeded line is present (a data-driven guard -- absent in a
-- fact-only build, it passes trivially, like the opt-in dedup tests). Float tolerance
-- 1e-4 on ratios, 1e-6 on money.

SELECT
    project_id, plan_id, line_key,
    budget, allocated_to_date, actual_to_date,
    consumed_pct, pace, remaining_budget, extrapolated_spend
FROM {{ ref('plan_pacing_by_line') }}
WHERE line_key = 'line-digital-a'
  AND (
        ABS(budget             - 3000.0) > 1e-6
     OR ABS(allocated_to_date  - 1000.0) > 1e-6
     OR ABS(actual_to_date     - 1250.0) > 1e-6
     OR consumed_pct       IS NULL OR ABS(consumed_pct       - 0.4166666667) > 1e-4
     OR pace               IS NULL OR ABS(pace               - 0.25)         > 1e-4
     OR remaining_budget   IS NULL OR ABS(remaining_budget   - 1750.0)       > 1e-6
     OR extrapolated_spend IS NULL OR ABS(extrapolated_spend - 3750.0)       > 1e-6
  )
