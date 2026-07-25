-- Staging Strava -- Clubs domain snapshot (competitor + own clubs).
-- SNAPSHOT-ONLY source: Strava returns no history; the connector stamps one
--   dated row per club per snapshot_date. This staging supersedes to the latest
--   pull per (project_id, snapshot_date, club_id) grain.
-- AD-7: QUALIFY supersede -- the latest pull per grain wins. ULIDs are
--        lexicographically monotone, so ORDER BY pull_id DESC = most recent.
-- AD-4: member_count / following_count are NON-ADDITIVE point-in-time LEVELS
--        (aggregation_rule=latest). They are read as the LAST value in a window,
--        NEVER summed across snapshot_date or across clubs. They are NOT emitted
--        into the additive cross-source fact_daily_kpi (the GSC average_position
--        precedent: a non-additive metric has no honest SUM row there); the
--        dedicated fact_strava_club_snapshot mart carries them.
-- AD-9: NULL honnete -- an absent count stays NULL, never zero-filled.
-- Staging module-owned (AI-06 Option A -- external model-path).
--
-- GRAIN: one row per (project_id, snapshot_date, club_id).
{{ config(materialized='view') }}

WITH raw AS (
    SELECT *
    FROM {{ source('raw_strava', 'raw_strava_club_daily') }}
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY project_id, snapshot_date, club_id
        ORDER BY pull_id DESC
    ) = 1
)

SELECT
    raw.snapshot_date,
    raw.club_id,
    raw.club_name,
    raw.sport_type,
    raw.club_city,
    raw.club_state,
    raw.club_country,
    raw.is_private,
    raw.is_verified,
    raw.club_url,
    raw.is_own_club,
    -- NULL honnete (AD-9): counts absent from the API response stay NULL.
    raw.member_count,
    raw.following_count,
    raw.pull_id,
    raw.loaded_at,
    raw.project_id
FROM raw
