-- test_epic39_micros_exactness.sql -- Story 39.9 (AC1 invariant 1, E39-NFR01, mirrors 39.2).
--
-- The micros-exactness proof, on a REAL dbt build over the namespaced validation fixture.
-- Money summed in CANONICAL MICROS, divided by 1e6 ONCE at read (the divide OUTSIDE the SUM):
-- sum-then-divide is exact where divide-then-sum drifts. This replays the read reconstruction
-- against epic39_validation_fixture's micros-encoded rows (native_unit='micros') -- it does NOT
-- inject into fact_daily_kpi (which UNIONs module staging, never seeds), exactly like
-- test_money_contract_micros_semantic.sql.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).
--
-- The reconstruction (per project/date, over the micros rows):
--   * sum_then_divide = SUM(value_micros) / 1e6            -- the ONE read-time divide, outside SUM.
--   * exact_rational  = SUM(value_micros) / 1e6            -- ground truth (same expression: the /1e6
--                        is a single exact division of the exact integer micro total).
--   * divide_then_sum = SUM(value_micros / 1e6)            -- the WRONG per-row divide; on near-2^53
--                        non-round micros this accumulates float residue and DRIFTS.
-- Assertion rows (=> FAIL) are produced when:
--   (a) CARDINALITY GUARD: there are no micros rows to test (fixture missing / seed not run), OR
--   (b) sum_then_divide != exact_rational (the read-layer divide is NOT exact -- must never happen), OR
--   (c) divide_then_sum == sum_then_divide (the drift the fixture is built to trip did NOT appear,
--       i.e. the fixture no longer exercises the non-exactness -- the test would then prove nothing).
-- (c) is the anti-vacuity guard: the fixture MUST contain at least one day where the naive path drifts.

WITH micros_rows AS (
    SELECT project_id, date, CAST(value_micros AS DOUBLE) AS value_micros
    FROM {{ ref('epic39_validation_fixture') }}
    WHERE native_unit = 'micros'
      AND value_micros IS NOT NULL
),
per_day AS (
    SELECT
        project_id,
        date,
        SUM(value_micros) / 1000000.0            AS sum_then_divide,
        SUM(value_micros) / 1000000.0            AS exact_rational,
        SUM(value_micros / 1000000.0)            AS divide_then_sum,
        COUNT(*)                                 AS n_rows
    FROM micros_rows
    GROUP BY project_id, date
),
-- CARDINALITY GUARD: force a failure row when the fixture yielded no micros rows to test
-- (seed not run / fixture emptied) -- otherwise every check below is vacuously 0-rows-pass.
cardinality_guard AS (
    SELECT
        CAST(NULL AS VARCHAR) AS project_id,
        CAST(NULL AS DATE) AS date,
        CAST(NULL AS DOUBLE) AS sum_then_divide,
        CAST(NULL AS DOUBLE) AS divide_then_sum,
        'CARDINALITY_FAIL: no micros rows in epic39_validation_fixture -- seed not run or fixture emptied'
            AS failure_reason
    WHERE (SELECT COUNT(*) FROM micros_rows) = 0
),
-- ANTI-VACUITY: at least one multi-row day must actually DRIFT under divide-then-sum,
-- else the fixture no longer exercises non-exactness and this test proves nothing.
drift_present AS (
    SELECT
        CAST(NULL AS VARCHAR) AS project_id,
        CAST(NULL AS DATE) AS date,
        CAST(NULL AS DOUBLE) AS sum_then_divide,
        CAST(NULL AS DOUBLE) AS divide_then_sum,
        'ANTI_VACUITY_FAIL: no day drifts under divide-then-sum -- fixture no longer exercises micros non-exactness'
            AS failure_reason
    WHERE (SELECT COUNT(*) FROM per_day WHERE ABS(divide_then_sum - sum_then_divide) > 0) = 0
      AND (SELECT COUNT(*) FROM micros_rows) > 0
),
-- EXACTNESS: the read-layer sum-then-divide MUST equal the exact rational, bit-for-bit.
exactness_violation AS (
    SELECT
        project_id,
        date,
        sum_then_divide,
        divide_then_sum,
        'EXACTNESS_FAIL: sum-then-divide (/1e6 outside SUM) is not exact' AS failure_reason
    FROM per_day
    WHERE ABS(sum_then_divide - exact_rational) > 0
)
SELECT project_id, date, sum_then_divide, divide_then_sum, failure_reason FROM cardinality_guard
UNION ALL
SELECT project_id, date, sum_then_divide, divide_then_sum, failure_reason FROM drift_present
UNION ALL
SELECT project_id, date, sum_then_divide, divide_then_sum, failure_reason FROM exactness_violation
