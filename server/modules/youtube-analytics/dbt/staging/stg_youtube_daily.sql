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
--
-- AI-310 -- THE GRAIN IS NAMED, NOT LEFT TO A CONVENTION. Two report profiles
-- land in `raw_youtube_daily`: `channel_daily` reports one row per day for the
-- whole channel, `video_daily` one row per day PER VIDEO. Read together they are
-- the same views counted twice -- measured on production 2026-08-22, project
-- proj_01KZGCRSV2XACWRP3RSVNWWGBK, metric `views`, with the QUALIFY below:
-- 2026-08-19 channel 390 / videos 390 / naive sum 780, and the ratio is EXACTLY
-- 2.000 on every day of the window.
--
-- Four other connectors already answer this with a column called `data_level`
-- (`stg_google_ads_daily.sql:27`, meta-ads, microsoft-ads, amazon-ads), and each
-- mart series reads ONLY the rows of its own level. YouTube was the connector
-- that carried the distinction and never said its name: `video = ''` is a
-- convention every consumer had to know, and the connector's own MCP tool did
-- not (it declared a `report_profile` argument and ignored it).
--
-- DERIVED, NEVER LANDED, and that is the whole difference with the four above.
-- Their provider reports the level and the connector stamps it, so the raw row
-- carries it. Here the level IS `video`, already in the relation and already the
-- declaration each report profile makes in the manifest (`channel_daily` ->
-- [date, channel_id], `video_daily` -> [date, video, channel_id]). Deriving it
-- needs no migration, no ALTER on an append-only landing and no backfill, and it
-- cannot drift from the rows the way a stamped copy can. Same argument the
-- semantic reader makes for the same defect one layer over
-- (`query_execution._grain_restrictions`): what was already written is read.
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
    -- AI-310: CHANNEL | VIDEO. NULL is impossible by construction -- a landed row
    -- either carries a video id or does not -- so no COALESCE legacy branch is
    -- needed here, unlike the stamped `data_level` of meta-ads.
    CASE
        WHEN raw.video IS NULL OR raw.video = '' THEN 'CHANNEL'
        ELSE 'VIDEO'
    END AS data_level,
    raw.metric,
    raw.value,
    raw.pull_id,
    raw.loaded_at,
    raw.project_id
FROM raw
