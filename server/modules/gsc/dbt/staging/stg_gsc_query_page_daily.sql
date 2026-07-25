-- Staging: GSC query x page grain (Story 10.5 — cannibalisation).
-- Source: raw_gsc_daily (SAME raw table as stg_gsc_daily), filtered to rows where
-- ``query IS NOT NULL`` — only the new ``query_page_daily`` pull profile lands a
-- non-null query, so the existing page/country/device rows are naturally excluded.
--
-- WHY A SEPARATE STAGING MODEL (not folded into stg_gsc_daily):
-- stg_gsc_daily's grain is (project_id, date, page, country, device). Cannibalisation
-- needs the JOINT (query, page) grain on the SAME row — a query split across pages —
-- which the marginal page/country/device rows cannot express (the query<->page join is
-- lost once each dimension is aggregated on its own). This model keeps that joint grain.
--
-- AD-4: clicks + impressions are additive; average_position is NON-ADDITIVE, stored raw
-- per (query, page) row WITH impressions as weight. NEVER SUM average_position — the
-- cannibalisation resolver impression-weights it per page (same rule as semantic_avg_position).
--
-- Supersede semantics (AD-7): when several pulls cover the same grain
-- (project_id, date, query, page), the LATEST pull wins. ULIDs are lexicographically
-- monotonic, so ORDER BY pull_id DESC = newest first.
{{ config(materialized='view') }}

SELECT
    date,
    query,
    page,
    clicks,
    impressions,
    average_position,         -- raw value from GSC; non-additive; stored with impressions as weight
    pull_id,
    loaded_at,
    project_id
FROM {{ source('raw_gsc', 'raw_gsc_daily') }}
WHERE query IS NOT NULL
  -- GSC full coverage: pin the web surface (NULL == legacy rows == web). Image/video
  -- rows can carry a query too; they belong to stg_gsc_surface_daily, not here.
  AND (search_type IS NULL OR search_type = 'web')
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY project_id, date, query, page
    ORDER BY pull_id DESC
) = 1
