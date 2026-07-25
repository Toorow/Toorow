-- fact_youtube_daily: YouTube channel/video daily performance (long format).
--
-- All stored metrics are ADDITIVE, so this belongs in the additive day-grain
-- path. It lands in a DEDICATED long-format mart for v1 (grain project_id x date
-- x channel_id x video x metric); wiring these into the cross-source
-- fact_daily_kpi (as new canonical metrics + dim_metric rows, breakdown by
-- video/channel) is a follow-up, deferred while the central seeds/mart carry open
-- parallel-session merge conflicts (see ROLLOUT_NOTES).
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
    metric,
    value,
    pull_id,
    loaded_at
FROM {{ ref('stg_youtube_daily') }}
