-- Staging YouTube -- the named uploads of each tracked channel (video directory).
--
-- WHAT THIS RELATION IS FOR. The analytics relations only ever carry video IDS;
-- this one carries the NAMES, the publication dates and the durations -- read
-- from the PUBLIC Data API for the own channel and every tracked competitor
-- alike (capabilities/competitors.md, outbound binding). `published_at` is what
-- makes a recent-window comparison honest: views-per-day-since-publication,
-- never lifetime counters of different eras side by side.
--
-- GRAIN: one row per (project_id, date, channel_id, video) -- `date` is the day
-- the reading was taken (a directory snapshot, not a flow), and the latest pull
-- per grain wins (AD-7 supersede, same QUALIFY as the two sibling stagings).
--
-- `lifetime_views` is a public all-time counter: aggregation=latest,
-- non-additive, and deliberately NOT a KPI-mart metric. It exists here so a
-- reader can rank a directory, not so anyone sums it.
{{ config(materialized='view') }}

WITH raw AS (
    SELECT *
    FROM {{ source('raw_youtube', 'raw_youtube_video_directory') }}
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY project_id, date, channel_id, video
        ORDER BY pull_id DESC
    ) = 1
)

SELECT
    raw.date,
    raw.channel_id,
    raw.channel_title,
    raw.video,
    raw.video_title,
    raw.published_at,
    raw.duration_seconds,
    raw.lifetime_views,
    raw.is_own_channel,
    raw.pull_id,
    raw.loaded_at,
    raw.project_id
FROM raw
