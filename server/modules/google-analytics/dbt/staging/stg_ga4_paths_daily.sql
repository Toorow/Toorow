-- Staging: GA4 pages_daily_paths profile (Epic 10, story 10.3).
-- Source: raw_ga4_paths_daily (GA4 pagePath dimension -> page).
-- Most-viewed pages = screen_page_views by page. This profile carries ONLY
-- (date, page, screen_page_views) -- no country/device to normalize, so NO
-- normalize_dimension macros apply here.
-- AD-4: additive metric only (screen_page_views).
-- AD-7: pull_id propagated from raw for the provenance chain.
--
-- AI-53 HONESTY: screen_page_views comes from the GA4 screenPageViews metric,
-- which is DECLARED per the api-schema but NOT verified live in this story (no
-- GA4 property connected). The live confirmation is gated to AC 11 / AI-13
-- (Phase B). Fallback if absent: sessions by page.
--
-- TOP-N BOUNDED PARTITION (Task 1 / point dur 1): raw rows are already top-N
-- bounded by the pull (orderBys screenPageViews DESC + limit=page_top_n). The
-- long tail beyond page_top_n is DROPPED by design -- the page partition does
-- NOT sum to the connector daily total. Correct and documented on the mart block.
--
-- Parallel, non-interfering: NEW partition landing breakdown_dimension='page' in
-- fact_daily_kpi. Does NOT touch device_category / country / user_type totals.
--
-- NB the GSC connector also lands a breakdown_dimension='page' partition
-- (stg_gsc_daily). Those are distinct rows: fact_daily_kpi keys on connector, so
-- GA4 page rows carry connector='google-analytics' and GSC page rows carry
-- connector='gsc' -- no collision at the mart grain.

SELECT
    date,
    page,
    -- Additive metric -- stored as a plain value (AD-4)
    screen_page_views,
    pull_id,
    loaded_at,
    -- project_id propagated from raw (seeded rows -> 'default'; real pulls carry own id).
    project_id
FROM {{ source('raw_ga4', 'raw_ga4_paths_daily') }}
-- AD-7 supersede semantics: when several pulls cover the same
-- (project_id, date, page), the LATEST pull wins.
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY project_id, date, page
    ORDER BY pull_id DESC
) = 1
