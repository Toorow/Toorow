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
--
-- ===================== STORY 61.4: THE LABEL THAT WAS WRONG =================
-- `MAX(currency)` picked one of the line currencies and stamped it on the plan
-- total. Since every line of a plan carries `media_plans.currency`, that stamp
-- was always the PLAN's currency -- on a total whose actual side had been
-- converted into the PROJECT's. This model now carries the same four currency
-- columns as its two neighbours and applies the same rule: an amount is stated
-- only under the currency that produced it, and nothing composed of two
-- currencies is stated at all.

{{ config(materialized='view') }}

WITH line AS (
    SELECT * FROM {{ ref('plan_pacing_by_line') }}
),

grouped AS (
    SELECT
        project_id,
        plan_id,
        plan_version_id,
        MIN(plan_currency)                                 AS plan_currency,
        COUNT(DISTINCT plan_currency)                      AS plan_currency_count,
        MIN(actual_currency)                               AS actual_currency,
        COUNT(DISTINCT actual_currency)                    AS actual_currency_count,
        MIN(reporting_currency)                            AS reporting_currency,
        MIN(money_policy_version_id)                       AS money_policy_version_id,
        MIN(money_gap_code)                                AS money_gap_code,
        {{ toorow_bool_and('CASE WHEN is_plan_only THEN TRUE ELSE money_is_composable END') }}
                                                           AS lines_are_composable,
        {{ toorow_bool_or('CASE WHEN is_plan_only THEN FALSE ELSE actual_withheld END') }}
                                                           AS actual_withheld,
        MIN(native_currency)                               AS native_currency,
        MIN(fx_as_of_date_min)                             AS fx_as_of_date_min,
        MAX(fx_as_of_date_max)                             AS fx_as_of_date_max,
        MIN(fx_source)                                     AS fx_source,
        MIN(fx_tier)                                       AS fx_tier,
        MIN(fx_method)                                     AS fx_method,
        MAX(as_of_day)                                     AS as_of_day,
        SUM(budget_micros)                                 AS budget_micros,
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
    GROUP BY project_id, plan_id, plan_version_id
),

resolved AS (
    SELECT
        *,
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
    -- The plan's ONE currency, or nothing. It used to be MAX(currency), which is
    -- never nothing. A plan that states no actual holds only plan-currency
    -- amounts, so the plan's currency is its one currency.
    CASE WHEN actual_currency_one IS NULL OR plan_currency_one = actual_currency_one
         THEN plan_currency_one ELSE NULL END          AS currency,
    plan_currency_one                                     AS plan_currency,
    actual_currency_one                                   AS actual_currency,
    reporting_currency,
    money_policy_version_id,
    money_gap_code,
    money_is_composable,
    actual_withheld,
    native_currency,
    fx_as_of_date_min,
    fx_as_of_date_max,
    fx_source,
    fx_tier,
    fx_method,
    as_of_day,
    CASE WHEN plan_currency_count = 1
         THEN {{ fee_tax_from_micros('budget_micros') }} ELSE NULL END AS budget,
    CASE WHEN plan_currency_count = 1 THEN budget_micros ELSE NULL END AS budget_micros,
    CASE WHEN plan_currency_count = 1
         THEN {{ fee_tax_from_micros('allocated_micros') }} ELSE NULL END
                                                          AS allocated_to_date,
    CASE WHEN plan_currency_count = 1 THEN allocated_micros ELSE NULL END
                                                          AS allocated_micros,
    CASE WHEN actual_withheld OR actual_currency_count > 1 THEN NULL
         ELSE {{ fee_tax_from_micros('actual_micros_summed') }} END
                                                          AS actual_to_date,
    CASE WHEN actual_withheld OR actual_currency_count > 1 THEN NULL
         ELSE actual_micros_summed END                    AS actual_micros,
    CASE WHEN money_is_composable
         THEN {{ fee_tax_from_micros('(budget_micros - actual_micros_summed)') }}
         ELSE NULL END                                    AS remaining_budget,
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
