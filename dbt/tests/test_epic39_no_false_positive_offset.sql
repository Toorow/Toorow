-- test_epic39_no_false_positive_offset.sql -- Story 39.9 (AC3 timezone side, E39-NFR06, 39.8).
--
-- 39.8's NO-FALSE-POSITIVE invariant on a REAL dbt build: over the SHARED-timezone subset (both
-- src_a and src_b report 'cost' under Europe/Paris on 2026-07-01), the offset-signalling
-- machinery MUST raise NOTHING -- a single distinct known report timezone => no advisory, no
-- recorded offset. This is the timezone-side companion to the bit-identical totals proof: a total
-- with no cross-tz peer is untouched AND unflagged.
--
-- The replay mirrors test_epic39_day_offset_signalled but ASSERTS THE OPPOSITE outcome on the
-- shared-tz scenario: the signal must NOT fire (n_distinct_tz must be < 2). A cardinality guard
-- ensures the shared-tz subset is actually present (else the "no false positive" claim is vacuous).
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

WITH shared_tz_rows AS (
    SELECT
        project_id,
        date,
        metric,
        connector,
        report_timezone
    FROM {{ ref('epic39_validation_fixture') }}
    WHERE scenario = 'shared_tz'
      AND report_timezone IS NOT NULL
      AND report_timezone <> ''
),
signal AS (
    SELECT
        project_id,
        date,
        metric,
        COUNT(DISTINCT report_timezone) AS n_distinct_tz,
        COUNT(*) AS n_rows
    FROM shared_tz_rows
    GROUP BY project_id, date, metric
),
-- CARDINALITY GUARD: the shared-tz subset must exist AND actually contain >=2 rows sharing one
-- timezone (otherwise "no offset on shared tz" proves nothing). Fail if absent or single-row.
cardinality_guard AS (
    SELECT
        CAST(NULL AS VARCHAR) AS project_id,
        CAST(NULL AS DATE) AS date,
        CAST(NULL AS VARCHAR) AS metric,
        CAST(NULL AS BIGINT) AS n_distinct_tz,
        'CARDINALITY_FAIL: shared_tz subset missing or has <2 same-tz rows -- seed not run or fixture emptied'
            AS failure_reason
    WHERE (SELECT COUNT(*) FROM signal WHERE n_rows >= 2) = 0
),
-- FALSE-POSITIVE VIOLATION: any shared-tz day with >=2 distinct timezones would mean the signal
-- fired on data that shares one clock -- a false positive. Must never happen.
false_positive AS (
    SELECT
        project_id,
        date,
        metric,
        n_distinct_tz,
        'FALSE_POSITIVE_FAIL: offset signalled on shared-timezone data (n_distinct_tz >= 2)'
            AS failure_reason
    FROM signal
    WHERE n_distinct_tz >= 2
)
SELECT project_id, date, metric, n_distinct_tz, failure_reason FROM cardinality_guard
UNION ALL
SELECT project_id, date, metric, n_distinct_tz, failure_reason FROM false_positive
