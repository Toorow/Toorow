-- Story 16.1 double-count guard (AC6b, AD-4):
-- The canonical single-dimension partition per (project_id, date, connector, metric)
-- for GA4 is picked by MIN(breakdown_dimension). Adding the acquisition partitions
-- (session_source_medium, session_campaign, first_user_source_medium) MUST NOT change
-- that choice -- MIN must stay 'country' so metric_baselines / cross_source_conversions
-- keep summing a single full-coverage series (no double count).
--
-- All three new dims start with 's'/'f' > 'c', and 'country' < 'country>device', so
-- MIN is 'country'. This test PROVES it on the built mart (not just by analysis).
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass). We return any
-- (project_id, date, metric) whose GA4 MIN(breakdown_dimension) is NOT 'country'.
--
-- review-16-1 F-2 + central verification: the scope is restricted to the metrics the
-- acquisition axes actually carry ('conversions', 'sessions'). Metrics that NEVER had a
-- 'country' partition (e.g. screen_page_views, landed 'page'-only by story 10.3) are out
-- of scope -- their MIN legitimately differs and predates this story. The remaining
-- assumption (documented): every date carrying an acquisition row also carries a
-- 'country' row for these metrics -- true because both land from the same daily pull
-- cadence; if a canonical-partition day gap ever appears, THIS test failing is the
-- correct signal (the gap is a real problem for the canonical selection).

SELECT
    project_id,
    date,
    metric,
    MIN(breakdown_dimension) AS min_dim
FROM {{ ref('fact_daily_kpi') }}
WHERE connector = 'google-analytics'
  AND metric IN ('conversions', 'sessions')
GROUP BY project_id, date, metric
HAVING MIN(breakdown_dimension) <> 'country'
