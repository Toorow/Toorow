-- Story 61.4 (AI-266) — THE TEST THAT WAS MISSING WHILE THE NUMBER WAS WRONG.
--
-- For two years `plan_vs_actual_daily` put `media_plans.currency` on every row and
-- `plan_pacing_by_line` carried it up as `currency`, while `actual_to_date` came
-- from `fact_daily_kpi.value` — an amount `fx_convert_at_read` had already
-- converted into the currency the staging FX join targeted. A plan in USD on a
-- Project reporting in EUR was therefore served as a USD pacing composed of a USD
-- budget and a EUR spend, to the pacing route, the MCP card `mediaplan_pacing`,
-- `mediaplan_alerts` and the scheduler. Five dbt tests watched these marts and not
-- one compared the two currencies, because nothing anywhere did.
--
-- This is that comparison. A dbt singular test FAILS when it returns rows.
--
-- It is not a re-statement of the models: every clause below is the OPPOSITE of
-- what the model writes, so removing a guard from the model makes a clause here
-- produce rows. Proven by mutation on a real build, not by reading.

-- 1. A currency label must name the currency that PRODUCED the amount beside it.
--    `currency` is the row's ONE currency: it may name the plan's currency, and
--    only the plan's, and only while the row holds nothing in another one -- so
--    either no actual is stated, or the actual is in that same currency.
SELECT
    project_id, plan_id, line_key,
    currency, plan_currency, actual_currency, actual_to_date,
    'currency_declared_across_two_currencies' AS violation
FROM {{ ref('plan_pacing_by_line') }}
WHERE currency IS NOT NULL
  AND (currency IS DISTINCT FROM plan_currency
       OR (actual_currency IS NOT NULL AND actual_currency <> currency))

UNION ALL

-- 1b. And the converse, which is the half a "no false label" rule usually forgets:
--     a row whose two sides ARE the same currency must SAY so. A mart that
--     answered every currency question with NULL would satisfy clause 1 and be
--     useless.
SELECT
    project_id, plan_id, line_key,
    currency, plan_currency, actual_currency, actual_to_date,
    'currency_withheld_though_both_sides_agree' AS violation
FROM {{ ref('plan_pacing_by_line') }}
WHERE currency IS NULL
  AND plan_currency IS NOT NULL
  AND (actual_currency IS NULL OR actual_currency = plan_currency)

UNION ALL

-- 2. Nothing composed of the two sides may exist when they are not the same
--    currency. consumed_pct divides them, pace subtracts an allocation from an
--    actual, remaining_budget subtracts an actual from a budget.
SELECT
    project_id, plan_id, line_key,
    currency, plan_currency, actual_currency, actual_to_date,
    'composed_figure_across_two_currencies' AS violation
FROM {{ ref('plan_pacing_by_line') }}
WHERE (plan_currency IS DISTINCT FROM actual_currency)
  AND (consumed_pct IS NOT NULL OR pace IS NOT NULL OR remaining_budget IS NOT NULL)

UNION ALL

-- 3. An amount that could not be stated is not a smaller amount. A line that
--    withheld its actual must state none — and must therefore pace nothing.
SELECT
    project_id, plan_id, line_key,
    currency, plan_currency, actual_currency, actual_to_date,
    'withheld_actual_still_produced_a_figure' AS violation
FROM {{ ref('plan_pacing_by_line') }}
WHERE actual_withheld
  AND (actual_to_date IS NOT NULL
       OR pace IS NOT NULL
       OR consumed_pct IS NOT NULL
       OR extrapolated_spend IS NOT NULL)

UNION ALL

-- 4. THE FIXTURE MUST CONTAIN THE CASE. A guard nothing exercises is a comment.
--    `dbt/seeds/mediaplan/seed_plan_mirror.py` seeds a USD plan on a EUR Project
--    and a plan whose campaign is billed in a currency `fx_rates.csv` has no rate
--    for; if either disappears, this test stops proving anything and says so
--    rather than staying green. Guarded on the fixture's own project so a client
--    warehouse with neither case is not failed for it.
SELECT
    'default' AS project_id, NULL AS plan_id, NULL AS line_key,
    NULL AS currency, NULL AS plan_currency, NULL AS actual_currency,
    NULL AS actual_to_date,
    'fixture_no_longer_carries_a_currency_divergence' AS violation
-- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
-- accepted by both, keeps this guard a guard on either engine.
FROM (SELECT 1) AS one_row
WHERE EXISTS (SELECT 1 FROM {{ ref('plan_pacing_by_line') }} WHERE project_id = 'default')
  AND NOT EXISTS (
      SELECT 1 FROM {{ ref('plan_pacing_by_line') }}
      WHERE project_id = 'default'
        AND money_gap_code = 'plan_currency_mismatch'
  )

UNION ALL

SELECT
    'default' AS project_id, NULL AS plan_id, NULL AS line_key,
    NULL AS currency, NULL AS plan_currency, NULL AS actual_currency,
    NULL AS actual_to_date,
    'fixture_no_longer_carries_an_unresolved_rate' AS violation
-- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
-- accepted by both, keeps this guard a guard on either engine.
FROM (SELECT 1) AS one_row
WHERE EXISTS (SELECT 1 FROM {{ ref('plan_pacing_by_line') }} WHERE project_id = 'default')
  AND NOT EXISTS (
      SELECT 1 FROM {{ ref('plan_pacing_by_line') }}
      WHERE project_id = 'default'
        AND actual_withheld
        AND money_gap_code = 'fx_rate_unavailable'
  )

UNION ALL

-- 5. THE ROLLUPS OBEY THE SAME RULE, and this is the clause that catches the
--    false under-delivery. A channel one of whose lines withheld its actual would
--    otherwise sum the OTHER lines and pace them under the channel's name — which
--    is exactly how a missing exchange rate came to fire a
--    `mediaplan_pace_underdelivery` on a campaign that had spent normally.
SELECT
    project_id, plan_id, channel AS line_key,
    currency, plan_currency, actual_currency, actual_to_date,
    'channel_rollup_paced_over_a_withheld_line' AS violation
FROM {{ ref('plan_pacing_by_channel') }}
WHERE actual_withheld
  AND (actual_to_date IS NOT NULL OR pace IS NOT NULL OR consumed_pct IS NOT NULL)

UNION ALL

SELECT
    project_id, plan_id, channel AS line_key,
    currency, plan_currency, actual_currency, actual_to_date,
    'channel_currency_declared_across_two_currencies' AS violation
FROM {{ ref('plan_pacing_by_channel') }}
WHERE currency IS NOT NULL
  AND (currency IS DISTINCT FROM plan_currency
       OR (actual_currency IS NOT NULL AND actual_currency <> currency))

UNION ALL

SELECT
    project_id, plan_id, CAST(NULL AS {{ toorow_string_type() }}) AS line_key,
    currency, plan_currency, actual_currency, actual_to_date,
    'plan_rollup_paced_over_a_withheld_line' AS violation
FROM {{ ref('plan_pacing_by_plan') }}
WHERE actual_withheld
  AND (actual_to_date IS NOT NULL OR pace IS NOT NULL OR consumed_pct IS NOT NULL)

UNION ALL

SELECT
    project_id, plan_id, CAST(NULL AS {{ toorow_string_type() }}) AS line_key,
    currency, plan_currency, actual_currency, actual_to_date,
    'plan_currency_declared_across_two_currencies' AS violation
FROM {{ ref('plan_pacing_by_plan') }}
WHERE currency IS NOT NULL
  AND (currency IS DISTINCT FROM plan_currency
       OR (actual_currency IS NOT NULL AND actual_currency <> currency))
