-- Staging: GSC non-web search surfaces (GSC full coverage).
-- Source: raw_gsc_daily (SAME raw table as stg_gsc_daily), filtered to rows landed
-- by the surface profiles (discover_daily, google_news_daily, news_daily,
-- image_daily, video_daily) — search_type NOT NULL and != 'web'. Discover and
-- Google News reporting data is ONLY reachable through the API 'type' parameter,
-- so these rows are the sole representation of those surfaces in the warehouse.
--
-- WHY A SEPARATE STAGING MODEL (not folded into stg_gsc_daily):
-- stg_gsc_daily's grain is web-only (project_id, date, page, country, device).
-- Surface rows describe a DIFFERENT data universe (a Discover impression is not a
-- web-search impression); mixing them would corrupt web day totals. Grain here is
-- (project_id, date, search_type, page).
--
-- AD-4: clicks + impressions are additive; average_position is NON-ADDITIVE, stored
-- raw per row WITH impressions as weight. NEVER SUM average_position.
-- (For Discover, 'position' is not meaningful — GSC still returns a value; consumers
-- should prefer clicks/impressions for that surface.)
--
-- Supersede semantics (AD-7): latest pull wins per grain; ULIDs are lexicographically
-- monotonic, so ORDER BY pull_id DESC = newest first.
{{ config(materialized='view') }}

SELECT
    date,
    search_type,
    page,
    clicks,
    impressions,
    average_position,         -- raw value from GSC; non-additive; weight = impressions
    pull_id,
    loaded_at,
    project_id
FROM {{ source('raw_gsc', 'raw_gsc_daily') }}
WHERE search_type IS NOT NULL
  AND search_type != 'web'
  AND search_appearance IS NULL
  AND hour IS NULL
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY project_id, date, search_type, page
    ORDER BY pull_id DESC
) = 1
