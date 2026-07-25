-- Staging: Google Ad Manager raw report data -> canonical names
-- AD-7: QUALIFY supersede -- latest pull per grain wins (ULIDs are lex-monotonic).
-- MONEY: `value` stays in MICROS through staging AND the mart (decision 2026-07-22).
--   The /1e6 happens ONCE at read, with `currency`. Do NOT divide here.
-- AD-4: ratios (CTR, eCPM, CPM/CPC rate) are NOT stored -- reconstructed in the mart
--   from additive components. `currency` + `report_timezone` carry the money/date
--   context so revenue is never naked and currencies are never summed together.
-- TODO(catalogue port): confirm the breakdown grain against the ported report profiles.
{{ config(materialized='view') }}

SELECT
    date,
    metric,
    value,
    currency,
    report_timezone,
    breakdown_dimension,
    breakdown_value,
    pull_id,
    loaded_at,
    project_id
FROM {{ source('raw_google_ad_manager', 'raw_google_ad_manager_daily') }}
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY project_id, date, metric, breakdown_dimension, breakdown_value
    ORDER BY pull_id DESC
) = 1
