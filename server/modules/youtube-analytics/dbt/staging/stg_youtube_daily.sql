-- Staging YouTube Analytics -- reports.query channel/video daily (long format).
-- AD-4: additive metrics only (views, estimated_minutes_watched, likes, comments,
--        shares, subscribers_gained, subscribers_lost). SUM valid at day grain.
--        Ratio metrics (average view duration/percentage, cpm) are NOT stored --
--        computed at the semantic layer from the additive numerators/denominators
--        (averageViewDuration = estimated_minutes_watched*60 / views).
-- AD-7: QUALIFY supersede -- latest pull per grain wins (ULIDs lex-monotone).
-- AD-9: NULL honnete -- an absent metric stays NULL, never zero-filled.
-- Staging module-owned (AI-06 Option A -- external model-path).
--
-- GRAIN: one row per (project_id, date, channel_id, video, metric). For the
-- channel_daily profile video is '' (empty); video_daily carries the video id.
{{ config(materialized='view') }}

WITH raw AS (
    SELECT *
    FROM {{ source('raw_youtube', 'raw_youtube_daily') }}
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY project_id, date, channel_id, video, metric
        ORDER BY pull_id DESC
    ) = 1
)

SELECT
    raw.date,
    raw.channel_id,
    raw.video,
    raw.metric,
    raw.value,
    raw.pull_id,
    raw.loaded_at,
    raw.project_id
FROM raw
