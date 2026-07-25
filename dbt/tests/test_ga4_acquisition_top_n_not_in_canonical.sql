-- Story 16.1 double-count guard (AC6c/AC6d, AD-4):
-- The acquisition partitions are TOP-N BOUNDED (the long tail beyond top_n is dropped
-- by design). A top-N-bounded partition has a PARTIAL sum -- it must NEVER be chosen as
-- the canonical day-total partition, or the connector total would be UNDER-counted (the
-- review-epic-10 CRITICAL: a bounded partition undercounts the window).
--
-- The canonical partition for GA4 is 'country' (MIN(breakdown_dimension), proven by
-- test_ga4_acquisition_min_breakdown_stable.sql). This test asserts the CONSEQUENCE that
-- matters: for the additive metric 'conversions', each acquisition axis' daily SUM must
-- be <= the 'country' (full-coverage canonical) daily SUM -- a top-N partition cannot
-- exceed the true day total. If it did, either the top-N bound leaked or a double count
-- inflated the axis. (Equality is allowed when the seed catalog fits under top_n; the
-- guard is the <= upper bound, mirroring test_ga4_pages_reconciliation's 1.05 margin.)
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

-- review-16-1 F-1: guard BOTH additive metrics landed on the acquisition axes
-- (conversions AND sessions) -- the sessions axis is equally a subset of the day total
-- (the seed calibration bug was precisely a sessions magnitude ~1.7x the day total).

WITH country_totals AS (
    SELECT project_id, date, metric, SUM(value) AS country_total
    FROM {{ ref('fact_daily_kpi') }}
    WHERE connector = 'google-analytics'
      AND metric IN ('conversions', 'sessions')
      AND breakdown_dimension = 'country'
    GROUP BY project_id, date, metric
),

acq_totals AS (
    SELECT project_id, date, metric, breakdown_dimension, SUM(value) AS axis_total
    FROM {{ ref('fact_daily_kpi') }}
    WHERE connector = 'google-analytics'
      AND metric IN ('conversions', 'sessions')
      AND breakdown_dimension IN (
          'session_source_medium', 'session_campaign', 'first_user_source_medium'
      )
    GROUP BY project_id, date, metric, breakdown_dimension
)

SELECT
    a.project_id,
    a.date,
    a.metric,
    a.breakdown_dimension,
    a.axis_total,
    c.country_total
FROM acq_totals a
JOIN country_totals c
    ON c.project_id = a.project_id AND c.date = a.date AND c.metric = a.metric
-- 1.05 noise margin (seeds generated independently of the standard seed; a true
-- double count would push an axis to ~2x the connector total). Same margin as the
-- pages reconciliation guard.
WHERE a.axis_total > c.country_total * 1.05
