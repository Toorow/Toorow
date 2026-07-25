-- plan_pacing_by_plan: per-plan pacing rollup over plan_pacing_by_line
-- (Epic 22, Story 22.4 / CAP-26 / FR38).
--
-- ============================ THIS MART IS A READ ============================
-- Reads plan_pacing_by_line only. Lands no fact row, edits no existing model.
--
-- GRAIN (enforced by plan_pacing_by_plan_grain_unique):
--   one row per (project_id, plan_id, plan_version_id).
--   NEVER aggregates across plans (décision 5): a campaign shared by two concurrent
--   plans is paced INDEPENDENTLY per plan; there is no cross-plan sum here (that
--   would double-count and needs a dedicated dedup -- Phase B / non-goal).
--
-- ===================== ADDITIVE ROLLUP + RATIOS AT VIEW LEVEL (AD-4) =========
-- Same discipline as plan_pacing_by_channel: only additive columns are summed;
-- consumed_pct / pace are re-derived from the sums at the view level (never an
-- average of per-line ratios). plan-only lines contribute budget but not pacing.

{{ config(materialized='view') }}

WITH line AS (
    SELECT * FROM {{ ref('plan_pacing_by_line') }}
),

grouped AS (
    SELECT
        project_id,
        plan_id,
        plan_version_id,
        MAX(currency)                                      AS currency,
        MAX(as_of_day)                                     AS as_of_day,
        SUM(budget)                                        AS budget,
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
    GROUP BY project_id, plan_id, plan_version_id
)

SELECT
    project_id,
    plan_id,
    plan_version_id,
    currency,
    as_of_day,
    budget,
    allocated_to_date,
    actual_to_date,
    budget - actual_to_date                               AS remaining_budget,
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
