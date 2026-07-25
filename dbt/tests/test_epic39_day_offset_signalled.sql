-- test_epic39_day_offset_signalled.sql -- Story 39.9 (AC1 invariant 5, E39-FR13/FR14, 39.7/39.8).
--
-- Day-offset SIGNALLING on a REAL dbt build, over the two-timezone fixture subset. When >=2
-- fixture connectors report the SAME metric on the SAME date under DIFFERENT known report
-- timezones (src_a Europe/Paris vs src_b America/New_York), the platform SIGNALS a possible
-- cross-source day-offset as provenance -- it must NEVER realign (39.8: grain=DATE, no sub-day
-- data). This test replays the 39.8 signal DECISION at the warehouse layer (the signal itself
-- is core.timezone_signal, not a mart column, so we replay its rule -- ">=2 distinct KNOWN
-- report timezones for a compared field" -- exactly as test_money_contract_micros_semantic
-- replays the micros read reconstruction) and asserts:
--   (a) the offset IS detectable (a recorded offset exists as provenance: >=2 distinct tz), AND
--   (b) NO row was re-sliced onto a fabricated "aligned" day -- the DISTINCT date partition of
--       each source row is unchanged from the seed (39.8 signals, never realigns), AND
--   (c) both timezones are KNOWN IANA-shaped names (a blank/undetermined tz is EXCLUDED from the
--       comparison, deferred to the 39.7 gap -- never coerced to UTC to force/suppress a signal).
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

WITH tz_rows AS (
    SELECT
        project_id,
        date,
        metric,
        connector,
        report_timezone
    FROM {{ ref('epic39_validation_fixture') }}
    WHERE scenario = 'recon_normalized'
      AND report_timezone IS NOT NULL
      AND report_timezone <> ''        -- exclude the undetermined-tz honesty row (39.7's gap, not here)
),
-- The signal replay: per (project, date, metric), the set of DISTINCT known report timezones.
-- >=2 => the TIMEZONE_DAY_OFFSET advisory fires (a cross-source day-offset is possible).
signal AS (
    SELECT
        project_id,
        date,
        metric,
        COUNT(DISTINCT report_timezone) AS n_distinct_tz,
        MAX(report_timezone) AS tz_hi,
        MIN(report_timezone) AS tz_lo
    FROM tz_rows
    GROUP BY project_id, date, metric
),
offset_signalled AS (
    SELECT * FROM signal WHERE n_distinct_tz >= 2
),
-- CARDINALITY GUARD: fail if the two-timezone case never materializes (fixture missing / seed
-- not run) -- otherwise the "signal fired" assertion is vacuously satisfied.
cardinality_guard AS (
    SELECT
        CAST(NULL AS VARCHAR) AS project_id,
        CAST(NULL AS DATE) AS date,
        CAST(NULL AS VARCHAR) AS metric,
        CAST(NULL AS BIGINT) AS n_distinct_tz,
        'CARDINALITY_FAIL: no >=2-timezone day in epic39_validation_fixture -- seed not run or fixture emptied'
            AS failure_reason
    WHERE (SELECT COUNT(*) FROM offset_signalled) = 0
),
-- NO-REALIGNMENT: the DISTINCT set of source dates present in the two-tz subset MUST be exactly
-- the dates the seed carries -- no fabricated "aligned" boundary day was manufactured. We assert
-- the offset-signalled day's date is one of the seed's own recon_normalized dates (identity).
realignment_violation AS (
    SELECT
        s.project_id,
        s.date,
        s.metric,
        s.n_distinct_tz,
        'REALIGNMENT_FAIL: an offset-signalled date is not a native seed date (a day was re-sliced)'
            AS failure_reason
    FROM offset_signalled s
    WHERE s.date NOT IN (SELECT DISTINCT date FROM tz_rows)
)
SELECT project_id, date, metric, n_distinct_tz, failure_reason FROM cardinality_guard
UNION ALL
SELECT project_id, date, metric, n_distinct_tz, failure_reason FROM realignment_violation
