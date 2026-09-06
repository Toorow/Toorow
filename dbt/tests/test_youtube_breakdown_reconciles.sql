-- AI-342 -- every YouTube breakdown series totals the SAME day as the channel
-- roll-up, and the canonical single series is still the channel one.
--
-- WHY THIS TEST AND NOT A ROW COUNT. The repair AI-342 asks for is that a
-- dimension a published Semantic View binds EXISTS in the relation its bindings
-- name. Landing rows is the easy half; landing rows that add up is the half that
-- decides whether the answer can be read beside any other answer. Each breakdown
-- of `fact_daily_kpi` is a PARALLEL series that independently totals the day
-- (the property `test_composite_reconciliation.sql` asserts for GA4), and marts
-- needing a day total pick one series via MIN(breakdown_dimension). If a YouTube
-- breakdown did not total its day, that selection would return a different
-- number depending on which dimension sorted first -- which is the exact xN
-- failure mode `datastream_projection.compile_projection` refuses at compile
-- time and which no test asserted on this connector's own rows.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).
--
-- SCOPE. Only the six breakdown dimensions that carry an ADDITIVE metric are
-- compared. `age_group` and `gender` carry `viewer_percentage` alone and reach
-- no row of this fact by construction, so there is nothing here to compare them
-- to. Only days the channel series also reports are compared: a breakdown pulled
-- for a day the channel report has not landed is a coverage fact, not an
-- arithmetic one, and this test must not turn one into the other.

WITH channel_totals AS (
    SELECT project_id, date, metric, SUM(value) AS channel_total
    FROM {{ ref('fact_daily_kpi') }}
    WHERE connector = 'youtube-analytics'
      AND breakdown_dimension = 'channel_id'
    GROUP BY project_id, date, metric
),

breakdown_totals AS (
    SELECT project_id, date, metric, breakdown_dimension, SUM(value) AS breakdown_total
    FROM {{ ref('fact_daily_kpi') }}
    WHERE connector = 'youtube-analytics'
      AND breakdown_dimension IN (
          'country', 'device_type', 'operating_system',
          'playback_location_type', 'subscribed_status', 'traffic_source_type'
      )
    GROUP BY project_id, date, metric, breakdown_dimension
)

SELECT
    b.project_id,
    b.date,
    b.metric,
    b.breakdown_dimension,
    b.breakdown_total,
    c.channel_total
FROM breakdown_totals b
JOIN channel_totals c
    ON c.project_id = b.project_id AND c.date = b.date AND c.metric = b.metric
WHERE ABS(b.breakdown_total - c.channel_total) > 0.001
