-- fact_gbp_location_daily: Google Business Profile daily location performance.
--
-- All 11 metrics are ADDITIVE daily counts, so this data belongs in the additive
-- day-grain path. It lands in a DEDICATED wide mart for v1 (one row per
-- project_id x date x location_id); wiring these metrics into the cross-source
-- long-format fact_daily_kpi (as new additive canonical metrics + dim_metric
-- rows) is a follow-up, deliberately deferred while the central seeds/mart carry
-- open parallel-session merge conflicts (see ROLLOUT_NOTES).
--
-- total_impressions is NOT stored as a column: it is the SUM of the 4
-- business_impressions_* metrics, computed downstream (never a raw metric).
{{ config(materialized='view') }}

SELECT
    project_id,
    date,
    location_id,
    business_impressions_desktop_maps,
    business_impressions_desktop_search,
    business_impressions_mobile_maps,
    business_impressions_mobile_search,
    business_conversations,
    business_direction_requests,
    call_clicks,
    website_clicks,
    business_bookings,
    business_food_orders,
    business_food_menu_clicks,
    pull_id,
    loaded_at
FROM {{ ref('stg_gbp_location_daily') }}
