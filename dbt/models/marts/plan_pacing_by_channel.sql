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
--
-- ===================== STORY 61.4: A ROLLUP CANNOT OUT-STATE ITS MEMBERS ====
-- Two defects lived here, and both were the same shape as the one in
-- `plan_vs_actual_daily` -- a rollup that skips what it cannot state:
--
--   * `SUM(actual_to_date)` skipped a line whose spend could not be converted, so
--     the channel total was the sum of the OTHER lines wearing the whole
--     channel's name, and its `pace` accused everybody. A channel now states no
--     actual when any of its paceable lines withheld one;
--   * this model carried NO CURRENCY COLUMN AT ALL. It served `budget`,
--     `actual_to_date` and `remaining_budget` with nothing anywhere saying what
--     they were denominated in. A channel now states its currency, or refuses to
--     compose -- a channel whose lines are in two currencies sums nothing.

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
        -- COUNT(DISTINCT) rather than MAX: a channel whose lines disagree on a
        -- currency has no single one, and picking either would be the exact
        -- fault this story repairs. Resolved below.
        MIN(plan_currency)                                 AS plan_currency,
        COUNT(DISTINCT plan_currency)                      AS plan_currency_count,
        MIN(actual_currency)                               AS actual_currency,
        COUNT(DISTINCT actual_currency)                    AS actual_currency_count,
        MIN(reporting_currency)                            AS reporting_currency,
        MIN(money_gap_code)                                AS money_gap_code,
        {{ toorow_bool_and('CASE WHEN is_plan_only THEN TRUE ELSE money_is_composable END') }}
                                                           AS lines_are_composable,
        {{ toorow_bool_or('CASE WHEN is_plan_only THEN FALSE ELSE actual_withheld END') }}
                                                           AS actual_withheld,
        MIN(fx_as_of_date_min)                             AS fx_as_of_date_min,
        MAX(fx_as_of_date_max)                             AS fx_as_of_date_max,
        MIN(fx_source)                                     AS fx_source,
        MIN(fx_tier)                                       AS fx_tier,
        MIN(fx_method)                                     AS fx_method,
        SUM(budget_micros)                                 AS budget_micros,
        -- allocated/actual to-date summed ONLY over paceable (non-plan-only) lines:
        -- a plan-only line has NULL actual and its allocation must not inflate the
        -- channel's pace denominator (it is not being paced).
        SUM(CASE WHEN is_plan_only THEN NULL ELSE allocated_micros END)
                                                           AS allocated_micros,
        SUM(CASE WHEN is_plan_only THEN NULL ELSE actual_micros END)
                                                           AS actual_micros_summed,
        SUM(CASE WHEN is_plan_only THEN NULL ELSE extrapolated_micros END)
                                                           AS extrapolated_micros_summed,
        SUM(CASE WHEN is_plan_only THEN 1 ELSE 0 END)      AS plan_only_line_count,
        SUM(CASE WHEN is_plan_only THEN 0 ELSE 1 END)      AS paceable_line_count,
        MIN(actual_pull_id_min)                            AS actual_pull_id_min,
        MAX(actual_pull_id_max)                            AS actual_pull_id_max,
        SUM(actual_pull_id_count)                          AS actual_pull_id_count
    FROM line
    GROUP BY project_id, plan_id, plan_version_id, COALESCE(channel, 'sans-canal')
),

resolved AS (
    SELECT
        *,
        -- One currency for the whole channel, or none. `plan_currency_count = 1`
        -- is what makes SUM(budget_micros) meaningful at all.
        CASE WHEN plan_currency_count = 1 THEN plan_currency ELSE NULL END
                                                           AS plan_currency_one,
        CASE WHEN actual_currency_count = 1 AND NOT actual_withheld
             THEN actual_currency ELSE NULL END            AS actual_currency_one,
        (lines_are_composable
         AND NOT actual_withheld
         AND plan_currency_count = 1
         AND actual_currency_count = 1)                    AS money_is_composable
    FROM grouped
)

SELECT
    project_id,
    plan_id,
    plan_version_id,
    channel,
    -- The channel's single currency, or nothing. This column did not exist. A
    -- channel that states no actual holds only plan-currency amounts, so the
    -- plan's currency is its one currency; a channel whose lines disagree on a
    -- plan currency has none, and `plan_currency_one` is already NULL there.
    CASE WHEN actual_currency_one IS NULL OR plan_currency_one = actual_currency_one
         THEN plan_currency_one ELSE NULL END          AS currency,
    plan_currency_one                                     AS plan_currency,
    actual_currency_one                                   AS actual_currency,
    reporting_currency,
    money_gap_code,
    money_is_composable,
    actual_withheld,
    fx_as_of_date_min,
    fx_as_of_date_max,
    fx_source,
    fx_tier,
    fx_method,
    -- A channel whose lines are in two plan currencies sums no budget: adding two
    -- currencies is not a total, it is a category error with a decimal point.
    CASE WHEN plan_currency_count = 1
         THEN {{ fee_tax_from_micros('budget_micros') }} ELSE NULL END AS budget,
    CASE WHEN plan_currency_count = 1 THEN budget_micros ELSE NULL END AS budget_micros,
    CASE WHEN plan_currency_count = 1
         THEN {{ fee_tax_from_micros('allocated_micros') }} ELSE NULL END
                                                          AS allocated_to_date,
    CASE WHEN plan_currency_count = 1 THEN allocated_micros ELSE NULL END
                                                          AS allocated_micros,
    -- A channel with NO paceable line has actual_to_date NULL (SUM over all-NULL is
    -- NULL in DuckDB/BigQuery) -> ratios NULL. Honest: nothing to pace. And a
    -- channel one of whose lines withheld its actual states none either.
    CASE WHEN actual_withheld OR actual_currency_count > 1 THEN NULL
         ELSE {{ fee_tax_from_micros('actual_micros_summed') }} END
                                                          AS actual_to_date,
    CASE WHEN actual_withheld OR actual_currency_count > 1 THEN NULL
         ELSE actual_micros_summed END                    AS actual_micros,
    -- remaining_budget = budget - actual (NULL when actual NULL: cannot know
    -- remainder; NULL across currencies: a subtraction is not a conversion).
    CASE WHEN money_is_composable
         THEN {{ fee_tax_from_micros('(budget_micros - actual_micros_summed)') }}
         ELSE NULL END                                    AS remaining_budget,
    -- ratios re-derived from the summed additive columns (AD-4), NULL-honest.
    CASE WHEN money_is_composable
         THEN {{ fee_tax_exact_ratio('actual_micros_summed', 'budget_micros') }}
         ELSE NULL END                                    AS consumed_pct,
    CASE WHEN money_is_composable
         THEN {{ fee_tax_exact_ratio('(actual_micros_summed - allocated_micros)', 'allocated_micros') }}
         ELSE NULL END                                    AS pace,
    CASE WHEN actual_withheld OR actual_currency_count > 1 THEN NULL
         ELSE {{ fee_tax_from_micros('extrapolated_micros_summed') }} END
                                                          AS extrapolated_spend,
    plan_only_line_count,
    paceable_line_count,
    actual_pull_id_min,
    actual_pull_id_max,
    actual_pull_id_count
FROM resolved
