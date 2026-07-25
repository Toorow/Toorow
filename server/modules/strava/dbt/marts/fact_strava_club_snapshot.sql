-- fact_strava_club_snapshot: DEDICATED snapshot mart for Strava club metrics.
--
-- WHY A DEDICATED MART (not fact_daily_kpi): member_count / following_count are
-- NON-ADDITIVE point-in-time LEVELS (aggregation_rule=latest). The central
-- fact_daily_kpi is additive-only (SUM); a non-additive level has no honest SUM
-- row there (exactly the GSC average_position precedent, which lives in a
-- semantic view, not the additive fact). So Strava club snapshots land here, one
-- row per (project_id, snapshot_date, club_id), value read as-is (last value per
-- grain already enforced by the staging QUALIFY supersede).
--
-- NON-ADDITIVITY CONTRACT: never SUM member_count/following_count across
-- snapshot_date or across clubs. The growth trend is member_count(t) -
-- member_count(t-1), derived at the semantic/card layer, null on the first
-- snapshot. The trend exists only from the first connection date onward (Strava
-- exposes no history).
--
-- is_own_club separates the connected athlete's own clubs from config-supplied
-- competitor clubs, so a card can compare "us vs competitors" without mixing.
{{ config(materialized='view') }}

SELECT
    project_id,
    snapshot_date,
    club_id,
    club_name,
    sport_type,
    club_city,
    club_state,
    club_country,
    is_private,
    is_verified,
    club_url,
    is_own_club,
    member_count,
    following_count,
    pull_id,
    loaded_at
FROM {{ ref('stg_strava_club_daily') }}
