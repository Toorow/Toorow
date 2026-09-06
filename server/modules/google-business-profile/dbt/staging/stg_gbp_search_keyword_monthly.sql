-- Staging Google Business Profile -- monthly search keywords.
--
-- MONTHLY grain, and it never meets the daily fact. The API publishes no daily
-- keyword breakdown, so any daily figure here would be invented; this model and
-- everything downstream of it stay on (month, location, keyword).
--
-- THE PRIVACY FLOOR SURVIVES INTACT. Google answers with EITHER an exact
-- `value` OR a `threshold` ("fewer than N") for a keyword under its privacy
-- floor. The two land in two columns and `is_thresholded` says which one is
-- real. Nothing here coalesces them: a floor read as a count is a fabricated
-- measurement, and it would be invisible once it entered a SUM.
--
-- AD-7: QUALIFY supersede -- the latest pull per grain wins.
--
-- GRAIN: one row per (project_id, month, location_id, search_keyword).
{{ config(materialized='view') }}

WITH raw AS (
    SELECT *
    FROM {{ source('raw_gbp', 'raw_gbp_search_keyword_monthly') }}
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY project_id, month, location_id, search_keyword
        ORDER BY pull_id DESC
    ) = 1
)

SELECT
    raw.month,
    raw.location_id,
    raw.search_keyword,
    -- Exact count, NULL when the keyword fell under the privacy floor.
    raw.search_keyword_impressions,
    -- The floor itself, NULL when an exact count came back. Never summed.
    raw.search_keyword_impressions_threshold,
    raw.is_thresholded,
    raw.pull_id,
    raw.loaded_at,
    raw.project_id
FROM raw
