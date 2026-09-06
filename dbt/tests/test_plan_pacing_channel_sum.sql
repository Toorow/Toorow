-- Story 22.4 channel-rollup exactness (AD-4, discriminant type 17.3): the
-- per-channel aggregate MUST equal the EXACT sum of its ventilated lines -- no double
-- count, no drift. "L'agrégation support == somme exacte des lignes ventilées."
--
-- For every (project, plan, version, channel):
--   plan_pacing_by_channel.actual_to_date == Σ plan_pacing_by_line.actual_to_date
--     over the channel's PACEABLE (non-plan-only) lines.
--   plan_pacing_by_channel.budget         == Σ plan_pacing_by_line.budget
--     over ALL the channel's lines (plan-only budgets included).
-- The ratios are re-derived at the view level (AD-4), never averaged -- that is
-- covered by the additive equality here (a correct additive base + view-level ratio
-- is the AD-4 contract).
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass). NULL-safe: a
-- fully-plan-only channel has actual_to_date NULL on BOTH sides (COALESCE to 0 for
-- the comparison). Float tolerance 1e-6.
--
-- AMENDED story 61.4. `SUM(actual_to_date)` on the LINE side skips a line that
-- withheld its actual, so this test used to require the channel to publish that
-- partial sum -- i.e. to publish the spend of the OTHER lines under the whole
-- channel's name. That is the false under-delivery, written as an assertion. A
-- channel one of whose paceable lines withheld now states no actual at all, and
-- the equality is asserted only where every member could be stated. What the
-- withheld channel must do instead is asserted in full by
-- test_plan_pacing_currency_is_declared.sql.

WITH line_rollup AS (
    SELECT
        project_id,
        plan_id,
        plan_version_id,
        COALESCE(channel, 'sans-canal') AS channel,
        SUM(budget)                                                       AS budget,
        SUM(CASE WHEN is_plan_only THEN NULL ELSE actual_to_date END)     AS actual_to_date,
        SUM(CASE WHEN is_plan_only THEN NULL ELSE allocated_to_date END)  AS allocated_to_date
    FROM {{ ref('plan_pacing_by_line') }}
    GROUP BY project_id, plan_id, plan_version_id, COALESCE(channel, 'sans-canal')
)

SELECT
    c.project_id, c.plan_id, c.plan_version_id, c.channel,
    c.actual_to_date AS channel_actual, lr.actual_to_date AS line_actual,
    c.budget         AS channel_budget, lr.budget         AS line_budget
FROM {{ ref('plan_pacing_by_channel') }} c
JOIN line_rollup lr
    ON  lr.project_id      = c.project_id
    AND lr.plan_id         = c.plan_id
    AND lr.plan_version_id = c.plan_version_id
    AND lr.channel         = c.channel
WHERE NOT c.actual_withheld
  AND (ABS(COALESCE(c.actual_to_date, 0) - COALESCE(lr.actual_to_date, 0)) > 1e-6
    OR ABS(COALESCE(c.budget, 0)         - COALESCE(lr.budget, 0))         > 1e-6
    OR ABS(COALESCE(c.allocated_to_date, 0) - COALESCE(lr.allocated_to_date, 0)) > 1e-6)
