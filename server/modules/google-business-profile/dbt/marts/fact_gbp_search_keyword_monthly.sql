-- fact_gbp_search_keyword_monthly: GBP search keywords, MONTHLY.
--
-- A DEDICATED MONTHLY MART, and the word monthly is the whole design. There is
-- no daily keyword breakdown at the source, so this fact never joins the daily
-- one: a keyword month cannot be spread over its days without inventing the
-- distribution, and the reconciliation that would follow would be arithmetic on
-- a fiction.
--
-- is_thresholded is carried, not resolved. A row with is_thresholded = true has
-- a NULL count and a FLOOR: "fewer than N", which is a bound on the truth and
-- not the truth. Any consumer summing search_keyword_impressions gets the exact
-- keywords only -- an UNDERSTATEMENT it can see coming, because the thresholded
-- rows are still there to be counted.
--
-- GRAIN: one row per (project_id, month, location_id, search_keyword).
{{ config(materialized='view') }}

SELECT
    project_id,
    month,
    location_id,
    search_keyword,
    search_keyword_impressions,
    search_keyword_impressions_threshold,
    is_thresholded,
    pull_id,
    loaded_at
FROM {{ ref('stg_gbp_search_keyword_monthly') }}
