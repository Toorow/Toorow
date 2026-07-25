-- plan_pacing_by_channel: per-channel pacing rollup over plan_pacing_by_line
-- (Epic 22, Story 22.4 / CAP-26 / FR38).
--
-- ============================ THIS MART IS A READ ============================
-- Reads plan_pacing_by_line only. Lands no fact row, edits no existing model.
--
-- GRAIN (enforced by plan_pacing_by_channel_grain_unique):
--   one row per (project_id, plan_id, plan_version_id, channel).
--   A NULL channel rolls up under the literal 'sans-canal' bucket so the grain key
--   is never NULL (honest bucket, never dropped).
--
-- ===================== ADDITIVE ROLLUP + RATIOS AT VIEW LEVEL (AD-4) =========
-- Only ADDITIVE columns are summed (budget, allocated_to_date, actual_to_date,
-- extrapolated_spend). The ratios (consumed_pct, pace) are RE-DERIVED from the
-- summed columns at the view level -- NEVER an average of the per-line ratios
-- (AD-4). This is the "agrégat support == somme exacte des lignes ventilées"
-- invariant: SUM(actual_to_date) over a channel == SUM of the ventilated line
-- spends (proven by test_plan_pacing_channel_sum.sql, a discriminant type 17.3).
--
-- ===================== PLAN-ONLY (decision 7, AD-9) ========================
-- is_plan_only lines contribute their BUDGET (a planned channel still has a budget)
-- but NOT actual/allocated-to-date pacing (their actual_to_date is NULL). We sum
-- actual_to_date with SUM() (NULLs skipped) so a channel that is entirely plan-only
-- has actual_to_date_sum = 0 over 0 pacing lines -> we NULL it out explicitly (a
-- fully plan-only channel has no honest actual, never a 0). plan_only_line_count is
-- surfaced so the card can badge it.

{{ config(materialized='view') }}

WITH line AS (
    SELECT * FROM {{ ref('plan_pacing_by_line') }}
),

grouped AS (
    SELECT
        project_id,
        plan_id,
        plan_version_id,
        COALESCE(channel, 'sans-canal')                    AS channel,
        SUM(budget)                                        AS budget,
        -- allocated/actual to-date summed ONLY over paceable (non-plan-only) lines:
        -- a plan-only line has NULL actual and its allocation must not inflate the
        -- channel's pace denominator (it is not being paced).
        SUM(CASE WHEN is_plan_only THEN NULL ELSE allocated_to_date END)
                                                           AS allocated_to_date,
        SUM(CASE WHEN is_plan_only THEN NULL ELSE actual_to_date END)
                                                           AS actual_to_date,
        SUM(CASE WHEN is_plan_only THEN NULL ELSE extrapolated_spend END)
                                                           AS extrapolated_spend,
        SUM(CASE WHEN is_plan_only THEN 1 ELSE 0 END)      AS plan_only_line_count,
        SUM(CASE WHEN is_plan_only THEN 0 ELSE 1 END)      AS paceable_line_count,
        MIN(actual_pull_id_min)                            AS actual_pull_id_min,
        MAX(actual_pull_id_max)                            AS actual_pull_id_max,
        SUM(actual_pull_id_count)                          AS actual_pull_id_count
    FROM line
    GROUP BY project_id, plan_id, plan_version_id, COALESCE(channel, 'sans-canal')
)

SELECT
    project_id,
    plan_id,
    plan_version_id,
    channel,
    budget,
    allocated_to_date,
    -- A channel with NO paceable line has actual_to_date NULL (SUM over all-NULL is
    -- NULL in DuckDB/BigQuery) -> ratios NULL. Honest: nothing to pace.
    actual_to_date,
    -- remaining_budget = budget - actual (NULL when actual NULL: cannot know remainder).
    budget - actual_to_date                               AS remaining_budget,
    -- ratios re-derived from the summed additive columns (AD-4), NULL-honest.
    actual_to_date / NULLIF(budget, 0)                    AS consumed_pct,
    (actual_to_date - allocated_to_date)
        / NULLIF(allocated_to_date, 0)                    AS pace,
    extrapolated_spend,
    plan_only_line_count,
    paceable_line_count,
    actual_pull_id_min,
    actual_pull_id_max,
    actual_pull_id_count
FROM grouped
