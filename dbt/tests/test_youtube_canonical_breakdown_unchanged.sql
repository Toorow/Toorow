-- AI-342 -- adding six breakdown series to youtube-analytics must not move the
-- canonical day-total series.
--
-- Marts and cards that need ONE day total per connector pick it with
-- MIN(breakdown_dimension) (`rollup.canonical_breakdown_per_connector`, and the
-- same rule written into every block of `fact_daily_kpi.sql`). Before AI-342
-- YouTube offered 'channel_id' and 'video', and MIN selected the channel
-- roll-up. The six dimensions added are 'country', 'device_type',
-- 'operating_system', 'playback_location_type', 'subscribed_status' and
-- 'traffic_source_type' -- every one of them sorts AFTER 'channel_id' -- but a
-- seventh whose name sorted before it would silently re-pin the hero KPI onto a
-- different series, which is the Epic-10 xN failure mode.
--
-- Comparing the strings would restate the sort I just made by hand; this asks
-- the WAREHOUSE which series MIN selects, so a dimension added tomorrow is
-- judged by the same operator the readers use.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

SELECT
    project_id,
    MIN(breakdown_dimension) AS canonical_breakdown
FROM {{ ref('fact_daily_kpi') }}
WHERE connector = 'youtube-analytics'
GROUP BY project_id
HAVING MIN(breakdown_dimension) <> 'channel_id'
