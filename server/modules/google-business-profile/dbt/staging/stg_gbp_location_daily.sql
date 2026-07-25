-- Staging Google Business Profile -- Performance API location_daily.
-- AD-4: all 11 DailyMetric values are ADDITIVE daily counts (impressions x
--        surface/device, actions). SUM is valid at day grain. Total impressions
--        = sum of the 4 business_impressions_* (no raw combined metric).
-- AD-7: QUALIFY supersede -- the latest pull per grain wins (ULIDs lex-monotone).
-- AD-9: NULL honnete -- a metric absent from the API response (zero-days are
--        OMITTED by GBP) stays NULL here; gap-fill to 0 is a downstream/mart
--        concern, never a fabricated raw value.
-- Staging module-owned (AI-06 Option A -- external model-path).
--
-- GRAIN: one row per (project_id, date, location_id).
{{ config(materialized='view') }}

WITH raw AS (
    SELECT *
    FROM {{ source('raw_gbp', 'raw_gbp_location_daily') }}
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY project_id, date, location_id
        ORDER BY pull_id DESC
    ) = 1
)

SELECT
    raw.date,
    raw.location_id,
    raw.business_impressions_desktop_maps,
    raw.business_impressions_desktop_search,
    raw.business_impressions_mobile_maps,
    raw.business_impressions_mobile_search,
    raw.business_conversations,
    raw.business_direction_requests,
    raw.call_clicks,
    raw.website_clicks,
    raw.business_bookings,
    raw.business_food_orders,
    raw.business_food_menu_clicks,
    raw.pull_id,
    raw.loaded_at,
    raw.project_id
FROM raw
