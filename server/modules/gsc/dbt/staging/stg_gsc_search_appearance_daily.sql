-- Staging: GSC searchAppearance dimension (GSC full coverage).
-- Source: raw_gsc_daily, filtered to rows landed by the search_appearance_daily
-- profile (search_appearance IS NOT NULL). The API forbids grouping
-- searchAppearance with any other dimension, so the connector loops one
-- single-day query per date and stamps the date on each row — the daily grain
-- here is reconstructed, not returned by the API.
--
-- Values are GSC search-appearance enums (e.g. RICHRESULT, AMP_BLUE_LINK, ...).
-- Grain: (project_id, date, search_appearance) — web search type only (the
-- profile pins type=web; rows carry search_type='web').
--
-- AD-4: clicks + impressions are additive; average_position is NON-ADDITIVE,
-- stored raw per row WITH impressions as weight. NEVER SUM average_position.
--
-- Supersede semantics (AD-7): latest pull wins per grain (ORDER BY pull_id DESC).
{{ config(materialized='view') }}

SELECT
    date,
    search_appearance,
    clicks,
    impressions,
    average_position,         -- raw value from GSC; non-additive; weight = impressions
    pull_id,
    loaded_at,
    project_id
FROM {{ source('raw_gsc', 'raw_gsc_daily') }}
WHERE search_appearance IS NOT NULL
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY project_id, date, search_appearance
    ORDER BY pull_id DESC
) = 1
