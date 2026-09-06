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
--
-- ===================== THE CURRENCIES (Story 61.4 / AI-266) =================
-- `plan_vs_actual_daily` decides the money gap once; this model only ROLLS IT UP
-- and applies its two consequences.
--
--   1. A to-date sum refuses when ANY of its days carried spend that could not be
--      stated (`actual_withheld`). Without this the SUM would skip that day's
--      NULL and produce a smaller number that looks measured -- the mechanism
--      that made `mediaplan_alerts` accuse a campaign of under-delivering because
--      an FX rate was missing.
--   2. NOTHING COMPOSED OF THE TWO SIDES IS PRODUCED when the plan's currency is
--      not the currency the spend was converted into. `consumed_pct`, `pace` and
--      `remaining_budget` each mix a plan-currency amount with a
--      reporting-currency one; a number composed that way is
--      `placement-mapping.md`'s "Incomplete if" in one cell. `budget`,
--      `allocated_to_date`, `actual_to_date` and `extrapolated_spend` are NOT
--      composed -- each is in one currency and keeps it -- so they stay, and the
--      row names the two currencies it holds.
--
-- The ratios are computed with `fee_tax_exact_ratio` over the micros, so two
-- warehouses print the same digits for the same two integers.

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
        -- `plan_currency` and `reporting_currency` are constant per line by
        -- construction (they come from the plan and from the Project), so MAX is
        -- a pick, not a choice. `actual_currency` is NOT: it is NULL on a day
        -- that states no actual, so MAX returns the currency of the days that DO
        -- state one, which is what the line needs. `currency` is NOT taken from
        -- the daily rows at all -- MAX over a column that is the plan's currency
        -- on every actual-less day would resurrect the very label this story
        -- removes on a line whose stated days disagree. It is recomputed below,
        -- once, at this grain.
        MAX(b.plan_currency)                       AS plan_currency,
        MAX(b.actual_currency)                     AS actual_currency,
        MAX(b.reporting_currency)                  AS reporting_currency,
        MAX(b.money_policy_version_id)             AS money_policy_version_id,
        MIN(b.money_gap_code)                      AS money_gap_code,
        MIN(b.native_currency)                     AS native_currency,
        MIN(b.fx_as_of_date_min)                   AS fx_as_of_date_min,
        MAX(b.fx_as_of_date_max)                   AS fx_as_of_date_max,
        MIN(b.fx_source)                           AS fx_source,
        MIN(b.fx_tier)                             AS fx_tier,
        MIN(b.fx_method)                           AS fx_method,
        MAX(b.budget_micros)                       AS budget_micros,
        MIN(b.line_start_date)                     AS line_start_date,
        MAX(b.line_end_date)                       AS line_end_date,
        -- The flag is constant per line; `toorow_bool_or` is the one spelling both
        -- engines accept (BigQuery has no BOOL_OR -- measured in production 2026-08-31).
        {{ toorow_bool_or('b.is_plan_only') }}     AS is_plan_only,
        MAX(b.sort_order)                          AS sort_order,
        a.as_of_day                                AS as_of_day,
        -- to-date sums (day <= plan-wide as_of_day). A line with no as_of (no plan
        -- spend at all) yields NULL sums via the CASE guards below.
        SUM(CASE WHEN a.as_of_day IS NOT NULL AND b.day <= a.as_of_day
                 THEN b.allocated_micros ELSE 0 END)                 AS allocated_micros,
        -- actual_to_date: SUM only over the REAL (non-NULL) contributions to-date.
        -- The inner CASE returns NULL (never 0) for days after as_of AND for days
        -- with no ventilated spend, so SUM (which skips NULLs) yields NULL when the
        -- line has NO real spend to-date -- including EVERY plan-only line (its
        -- actual_amount is NULL on every day) and any plan with no spend at all
        -- (as_of_day NULL). Honest "cannot pace", never a fabricated 0 (décision 7).
        SUM(CASE WHEN a.as_of_day IS NOT NULL AND b.day <= a.as_of_day
                 THEN b.actual_micros ELSE NULL END)                 AS actual_micros_summed,
        -- Story 61.4: TRUE when at least one to-date day of this line carried
        -- spend that could not be stated. The SUM above would have skipped it and
        -- returned a smaller number wearing the clothes of a measurement.
        {{ toorow_bool_or('CASE WHEN a.as_of_day IS NOT NULL AND b.day <= a.as_of_day THEN b.actual_withheld ELSE FALSE END') }}
                                                                     AS actual_withheld,
        -- provenance span over the ventilated fact rows of this line.
        MIN(b.actual_pull_id)                      AS actual_pull_id_min,
        MAX(b.actual_pull_id)                      AS actual_pull_id_max,
        COUNT(DISTINCT b.actual_pull_id)           AS actual_pull_id_count
    FROM base b
    JOIN as_of a
        ON a.project_id = b.project_id AND a.plan_id = b.plan_id
    GROUP BY b.project_id, b.plan_id, b.plan_version_id, b.line_key, a.as_of_day
),

-- The two decisions of this model, taken ONCE so no expression below can drift
-- from another: what the to-date actual is, and whether the two sides may be
-- composed at all.
withheld AS (
    SELECT
        *,
        CASE WHEN actual_withheld THEN NULL ELSE actual_micros_summed END
                                                                    AS actual_micros,
        -- The line's ONE currency, or nothing.
        CASE WHEN actual_currency IS NULL OR plan_currency = actual_currency
             THEN plan_currency ELSE NULL END                       AS currency,
        -- Composable = the budget's currency IS the currency the spend was
        -- converted into, and an actual exists to compose with.
        (NOT actual_withheld
         AND actual_currency IS NOT NULL
         AND plan_currency = actual_currency)                       AS money_is_composable
    FROM per_line
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
    FROM withheld
),

-- The run-rate extrapolation, in exact decimal rather than binary float, for the
-- reason `fee_tax_exact_ratio` states: an unqualified division promotes to DOUBLE
-- and the two adapters then print different low digits for the same two integers.
--
-- IT IS A MACRO NOW (AI-268). Written inline, it spelled its casts
-- `DECIMAL(p,s)` with no `target.type` branch -- DuckDB's spelling, which
-- BigQuery does not accept at all. Nothing was wrong on screen, because the
-- family is already DuckDB-only through an earlier CAST; the falseness sat in
-- the load, which is where 60.3 forbade it.
extrapolated AS (
    SELECT
        *,
        {{ fee_tax_run_rate_micros('actual_micros', 'days_total', 'days_elapsed') }}
                                                                    AS extrapolated_micros
    FROM elapsed
)

SELECT
    project_id,
    plan_id,
    plan_version_id,
    line_key,
    label,
    channel,
    -- `currency` is the ONE currency this row is in, or nothing. Before 61.4 it
    -- was `media_plans.currency` unconditionally, on a row whose actual had been
    -- converted into the Project's currency: the label named a currency that had
    -- not produced the number beside it.
    currency,
    plan_currency,
    actual_currency,
    reporting_currency,
    money_policy_version_id,
    money_gap_code,
    -- Stated rather than left to be re-derived by every reader: the console, the
    -- MCP card and the alert evaluator must all answer the same question the same
    -- way, and three re-derivations of one rule is how two of them drift.
    money_is_composable,
    actual_withheld,
    native_currency,
    fx_as_of_date_min,
    fx_as_of_date_max,
    fx_source,
    fx_tier,
    fx_method,
    {{ fee_tax_from_micros('budget_micros') }}                      AS budget,
    budget_micros,
    line_start_date,
    line_end_date,
    is_plan_only,
    sort_order,
    as_of_day,
    days_total,
    days_elapsed,
    {{ fee_tax_from_micros('allocated_micros') }}                   AS allocated_to_date,
    allocated_micros,
    -- plan-only lines: actual_to_date stays NULL -> every ratio below is NULL. So
    -- does a line one of whose to-date days could not be converted.
    {{ fee_tax_from_micros('actual_micros') }}                      AS actual_to_date,
    actual_micros,
    -- consumed_pct = actual / budget. NULL when budget=0 (NULLIF) or actual NULL --
    -- AND NULL when the two sides are in different currencies, because this
    -- division would then be a plain category error rendered as a percentage.
    CASE WHEN money_is_composable
         THEN {{ fee_tax_exact_ratio('actual_micros', 'budget_micros') }}
         ELSE NULL END                                              AS consumed_pct,
    -- pace = (actual - allocated_to_date) / allocated_to_date. NULL (never 0) when
    -- allocated_to_date = 0 -- the story's "un jour sans allocation donne Pace NULL".
    -- Same currency guard: the numerator subtracts two currencies otherwise.
    CASE WHEN money_is_composable
         THEN {{ fee_tax_exact_ratio('(actual_micros - allocated_micros)', 'allocated_micros') }}
         ELSE NULL END                                              AS pace,
    -- remaining_budget = budget - actual (NULL when actual NULL: cannot know
    -- remainder; NULL across currencies: a subtraction is not a conversion).
    CASE WHEN money_is_composable
         THEN {{ fee_tax_from_micros('(budget_micros - actual_micros)') }}
         ELSE NULL END                                              AS remaining_budget,
    -- extrapolated_spend = run-rate * days_total. NULL when days_elapsed=0 or
    -- actual NULL. NOT currency-guarded: it is the actual extrapolated over its
    -- own days, so it stays in `actual_currency` and composes nothing.
    {{ fee_tax_from_micros('extrapolated_micros') }}                AS extrapolated_spend,
    extrapolated_micros,
    actual_pull_id_min,
    actual_pull_id_max,
    actual_pull_id_count
FROM extrapolated
