-- fact_youtube_daily: YouTube channel/video daily performance (long format).
--
-- All stored metrics are ADDITIVE, so this belongs in the additive day-grain
-- path. It lands in a DEDICATED long-format mart for v1 (grain project_id x date
-- x channel_id x video x metric); wiring these into the cross-source
-- fact_daily_kpi (as new canonical metrics + dim_metric rows, breakdown by
-- video/channel) is a follow-up, deferred while the central seeds/mart carry open
-- parallel-session merge conflicts (see ROLLOUT_NOTES).
--
-- The two report grains that share the landing are NAMED here by `data_level`
-- (CHANNEL | VIDEO, derived in `stg_youtube_daily`): this mart is a passthrough
-- at row grain, so every SUM over it belongs to whoever reads it, and a reader
-- that cannot name the grain sums both.
--
-- Ratio metrics (average view duration/percentage, cpm) are NEVER in this mart:
-- averageViewDuration is recomputed at the semantic layer from
-- estimated_minutes_watched and views (AD-4), like CTR from clicks/impressions.
{{ config(materialized='view') }}

SELECT
    project_id,
    date,
    channel_id,
    video,
    -- AI-310: the grain marker travels with the fact. A consumer of this mart
    -- reads two report grains in one relation, and until this column existed it
    -- could only tell them apart by knowing that `video = ''` means the channel
    -- roll-up. `get_youtube_analytics_report` did not know it, and answered a
    -- `report_profile=channel_daily` question with both grains -- exactly 2x.
    data_level,
    metric,
    value,
    pull_id,
    loaded_at
FROM {{ ref('stg_youtube_daily') }}
