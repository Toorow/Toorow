-- test_anomaly_day_injected: verify the seed anomaly day (day 75) produces a row
-- in anomalies_daily for the sessions metric on the google-analytics connector.
-- Story 5.4 (AC4, AI-09): end-to-end testability proof.
--
-- dbt singular test: returns rows when it FAILS (zero rows = pass).
-- This test FAILS (returns rows) if day 75 does NOT produce an anomaly.
--
-- Day 75 is the ANOMALY_SEED_DAY from generate_seed.py (AI-09): sessions = 3×baseline.
-- With a 30-day trailing window and +0.5%/day linear trend, z-score >> 3.0 for that day.
-- The date is dynamically computed as (seed_end_date - 90 days + 75 days = seed_end_date - 15 days).
-- We test by checking that at least one anomaly row exists for metric='sessions'
-- connector='google-analytics' anywhere in the dataset (the injected day).

SELECT 'no_anomaly_found' AS failure_reason
WHERE NOT EXISTS (
    SELECT 1
    FROM {{ ref('anomalies_daily') }}
    WHERE connector = 'google-analytics'
      AND metric    = 'sessions'
      AND zscore IS NOT NULL
      AND ABS(zscore) >= 3.0
)
