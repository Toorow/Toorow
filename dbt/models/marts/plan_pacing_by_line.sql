-- plan_pacing_by_line: per-line pacing (to-date) over plan_vs_actual_daily
-- (Epic 22, Story 22.4 / CAP-26 / FR38 -- Given/When/Then chiffré de la story).
--
-- ============================ THIS MART IS A READ ============================
-- Reads plan_vs_actual_daily only (which itself reads the plan mirror + fact_daily_kpi
-- WITHOUT mutating a single fact). Lands no fact row, edits no existing model.
--
-- GRAIN (enforced by plan_pacing_by_line_grain_unique):
--   one row per (project_id, plan_id, plan_version_id, line_key).
--
-- ===================== "TO-DATE" ANCHOR (documented choice) =================
-- The to-date cutoff is the MAX day that carries REAL spend within the plan
-- (as_of_day = MAX(day WHERE actual_amount IS NOT NULL) over the whole plan). This
-- is a DETERMINISTIC, warehouse-data-driven anchor -- NOT SQL CURRENT_DATE (which is
-- non-deterministic and would make tests time-dependent). Rationale (spike §3,
-- décision 3): pacing compares the real PAST to the active plan; the newest day with
-- real data is the honest "today" of the warehouse. Consequence:
--   * allocated_to_date = SUM(allocated_amount) for day <= as_of_day (per line);
--   * actual_to_date    = SUM(actual_amount)    for day <= as_of_day (per line).
-- When the plan has NO real spend at all (fresh plan / all plan-only), as_of_day is
-- NULL -> actual_to_date and every derived ratio are NULL (honest "cannot pace yet",
-- never a fabricated 0). The plan-wide anchor (not per-line) is used so every line is
-- measured against the SAME "today", including lines that have not yet spent.
--
-- ===================== FORMULAS (AD-4 ratios at view level, AD-9) ===========
--   consumed_pct     = actual_to_date / NULLIF(budget, 0)
--   pace             = (actual_to_date - allocated_to_date)
--                        / NULLIF(allocated_to_date, 0)   -- NULL when alloc-to-date=0
--   remaining_budget = budget - actual_to_date
--   extrapolated_spend = actual_to_date / NULLIF(days_elapsed, 0) * days_total
--                        (simple run-rate; NULL when days_elapsed = 0)
-- Every ratio is a ratio-of-sums computed at the VIEW level over additive columns
-- (AD-4) -- never an average of per-day ratios. NULLIF makes a zero denominator an
-- HONEST NULL, never a disguised 0 (décision 6/7, AD-9). The story's worked example
-- (3 000 EUR / 30 j, 10 j écoulés, 1 250 EUR -> consumed 41,7 %, pace +25 %,
-- remaining 1 750 EUR, extrapolated 3 750 EUR) is asserted by
-- test_plan_pacing_extrapolation.sql.
--
-- ===================== PLAN-ONLY (decision 7) ==============================
-- is_plan_only lines keep their budget + allocated_to_date but actual_to_date stays
-- NULL, so consumed_pct / pace / extrapolated_spend are ALL NULL (no pacing) -- the
-- honest plan-only contract, proven by test_plan_pacing_plan_only_actual_null.sql.
--
-- ===================== PROVENANCE (AD-9) ====================================
-- plan_version_id + MIN/MAX/COUNT DISTINCT of the underlying actual_pull_id (the
-- full provenance span + id count that fed the ventilated spend of this line).

{{ config(materialized='view') }}

WITH base AS (
    SELECT * FROM {{ ref('plan_vs_actual_daily') }}
),

-- Plan-wide "today": the newest day with real spend anywhere in the plan.
as_of AS (
    SELECT
        project_id,
        plan_id,
        MAX(CASE WHEN actual_amount IS NOT NULL THEN day END) AS as_of_day
    FROM base
    GROUP BY project_id, plan_id
),

per_line AS (
    SELECT
        b.project_id,
        b.plan_id,
        b.plan_version_id,
        b.line_key,
        MAX(b.label)                               AS label,
        MAX(b.channel)                             AS channel,
        MAX(b.currency)                            AS currency,
        MAX(b.budget)                              AS budget,
        MIN(b.line_start_date)                     AS line_start_date,
        MAX(b.line_end_date)                       AS line_end_date,
        -- BOOL_OR is portable (DuckDB + BigQuery): the flag is constant per line.
        BOOL_OR(b.is_plan_only)                    AS is_plan_only,
        MAX(b.sort_order)                          AS sort_order,
        a.as_of_day                                AS as_of_day,
        -- to-date sums (day <= plan-wide as_of_day). A line with no as_of (no plan
        -- spend at all) yields NULL sums via the CASE guards below.
        SUM(CASE WHEN a.as_of_day IS NOT NULL AND b.day <= a.as_of_day
                 THEN b.allocated_amount ELSE 0 END)                 AS allocated_to_date,
        -- actual_to_date: SUM only over the REAL (non-NULL) contributions to-date.
        -- The inner CASE returns NULL (never 0) for days after as_of AND for days
        -- with no ventilated spend, so SUM (which skips NULLs) yields NULL when the
        -- line has NO real spend to-date -- including EVERY plan-only line (its
        -- actual_amount is NULL on every day) and any plan with no spend at all
        -- (as_of_day NULL). Honest "cannot pace", never a fabricated 0 (décision 7).
        SUM(CASE WHEN a.as_of_day IS NOT NULL AND b.day <= a.as_of_day
                 THEN b.actual_amount ELSE NULL END)                 AS actual_to_date,
        -- provenance span over the ventilated fact rows of this line.
        MIN(b.actual_pull_id)                      AS actual_pull_id_min,
        MAX(b.actual_pull_id)                      AS actual_pull_id_max,
        COUNT(DISTINCT b.actual_pull_id)           AS actual_pull_id_count
    FROM base b
    JOIN as_of a
        ON a.project_id = b.project_id AND a.plan_id = b.plan_id
    GROUP BY b.project_id, b.plan_id, b.plan_version_id, b.line_key, a.as_of_day
),

elapsed AS (
    SELECT
        *,
        -- days_total = the line's full flight length (inclusive).
        {{ days_between('line_start_date', 'line_end_date') }} + 1  AS days_total,
        -- days_elapsed = days of the line up to and including as_of_day, clamped to
        -- the line window. 0 when the line has not started by as_of_day (or no spend
        -- at all) -> extrapolation NULL (honest, no run-rate from zero elapsed days).
        CASE
            WHEN as_of_day IS NULL THEN 0
            WHEN as_of_day < line_start_date THEN 0
            WHEN as_of_day >= line_end_date
                THEN {{ days_between('line_start_date', 'line_end_date') }} + 1
            ELSE {{ days_between('line_start_date', 'as_of_day') }} + 1
        END                                                         AS days_elapsed
    FROM per_line
)

SELECT
    project_id,
    plan_id,
    plan_version_id,
    line_key,
    label,
    channel,
    currency,
    budget,
    line_start_date,
    line_end_date,
    is_plan_only,
    sort_order,
    as_of_day,
    days_total,
    days_elapsed,
    allocated_to_date,
    -- plan-only lines: actual_to_date stays NULL -> every ratio below is NULL.
    actual_to_date,
    -- consumed_pct = actual / budget. NULL when budget=0 (NULLIF) or actual NULL.
    actual_to_date / NULLIF(budget, 0)                              AS consumed_pct,
    -- pace = (actual - allocated_to_date) / allocated_to_date. NULL (never 0) when
    -- allocated_to_date = 0 -- the story's "un jour sans allocation donne Pace NULL".
    (actual_to_date - allocated_to_date)
        / NULLIF(allocated_to_date, 0)                              AS pace,
    -- remaining_budget = budget - actual (NULL when actual NULL: cannot know remainder).
    budget - actual_to_date                                         AS remaining_budget,
    -- extrapolated_spend = run-rate * days_total. NULL when days_elapsed=0 or actual NULL.
    actual_to_date / NULLIF(days_elapsed, 0) * days_total           AS extrapolated_spend,
    actual_pull_id_min,
    actual_pull_id_max,
    actual_pull_id_count
FROM elapsed
