-- Staging: maps raw GSC source fields to canonical names
-- connector-requirements.md GSC profile
-- AD-4: clicks + impressions are additive; average_position is NON-ADDITIVE.
-- average_position is stored raw per row WITH impressions (the weight).
-- The semantic layer (semantic_avg_position view) applies the impression-weighted
-- aggregation. NEVER SUM average_position directly in this model or any mart.
-- AD-7: pull_id propagated from raw
-- Story 6.2: module-owned staging (AI-06 decision Option A -- external model-path).
--
-- Supersede semantics (AD-7): when several pulls cover the same grain
-- (project_id, date, page, country, device), the LATEST pull wins.
-- ULIDs are lexicographically monotonic, so ORDER BY pull_id DESC = newest first.
--
-- Device dimension vocabulary: raw values from GSC are already normalized to
-- lowercase (connector.py _DEVICE_CANONICAL_MAP: MOBILE->mobile, etc.) at ingest.
{{ config(materialized='view') }}

SELECT
    date,
    page,
    country,
    device,
    clicks,
    impressions,
    average_position,         -- raw value from GSC; non-additive; stored with impressions as weight
    pull_id,
    loaded_at,
    project_id
FROM {{ source('raw_gsc', 'raw_gsc_daily') }}
-- GRAIN ISOLATION (GSC full coverage fix): raw_gsc_daily is shared by every GSC
-- profile. Without these filters, foreign-grain rows collapse into the
-- (date, page, '', '') partition and corrupt the daily numbers:
--   query IS NULL              -> excludes query_page_daily rows (several queries per
--                                 page/day would dedupe against real page rows)
--   search_type NULL or 'web'  -> excludes discover/news/image/video/googleNews
--                                 surface rows (their own staging: stg_gsc_surface_daily)
--   search_appearance IS NULL  -> excludes searchAppearance rows (page='' rows that
--                                 would double-count the day under a '' page bucket)
--   hour IS NULL               -> excludes ad-hoc hourly pulls (partial data)
WHERE query IS NULL
  AND (search_type IS NULL OR search_type = 'web')
  AND search_appearance IS NULL
  AND hour IS NULL
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY project_id, date, page, country, device
    ORDER BY pull_id DESC
) = 1
