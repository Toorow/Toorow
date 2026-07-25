-- Staging: Piano Analytics LONG-format daily landing (Story 28.1).
-- AD-7: QUALIFY supersede -- the latest pull per grain wins. ULIDs are
-- lexicographically monotonic, so ORDER BY pull_id DESC = newest first.
--
-- GRAIN: one row per (project_id, date, site_id, segments_json, metric). The
-- landing is LONG (metric, value_num) because catalog_daily can select ANY
-- Data Model key (baseline OR per-site custom) -- wide columns cannot hold an
-- arbitrary selection. segments_json carries the selected non-key properties
-- (device_type, geo_country, src_source, page, event_name, custom_* ...) as
-- canonical sorted-key JSON so any dimension participates in the supersede
-- grain without schema churn.
--
-- AD-4: NON-ADDITIVE metrics (visits, unique_visitors, bounce_rate, ...) carry
-- non_additive=TRUE. They are VISIT/VISITOR-SCOPED and do NOT sum across
-- property breakdowns (Piano "Why can't we sum visits on page analyses?").
-- NEVER SUM them downstream; a breakdown sum != the site total (use getTotal /
-- recompute at the semantic layer). Landed raw at day grain.
--
-- Piano metrics are plain counts/decimals -- there is NO micro-currency
-- conversion (unlike the ads platforms). value_num is the value as returned.
{{ config(materialized='view') }}

SELECT
    date,
    site_id,
    segments_json,
    metric,
    value_num,
    non_additive,
    pull_id,
    loaded_at,
    project_id
FROM {{ source('raw_piano', 'raw_piano_daily') }}
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY project_id, date, site_id, COALESCE(segments_json, ''), metric
    ORDER BY pull_id DESC
) = 1
