-- Staging: GA4 acquisition_daily_first_user profile (Epic 16, story 16.1).
-- Source: raw_ga4_acquisition_first_user (GA4 firstUserSourceMedium, first-click axis).
-- Carries (date, first_user_source_medium, conversions) -- no country/device to
-- normalize, so NO normalize_dimension macros apply here.
-- AD-4: additive metric only (conversions).
-- AD-7: pull_id propagated from raw for the provenance chain.
--
-- TOP-N BOUNDED PARTITION (point dur 1): raw rows already top-N bounded by the pull
-- (orderBys conversions DESC + limit=top_n). Long tail DROPPED by design -- this
-- partition does NOT sum to the connector daily total. Documented on the mart.
--
-- SEPARATE from stg_ga4_acquisition_session: the first-click axis is a distinct
-- runReport (no session x first-user cross-join). Lands breakdown_dimension
-- 'first_user_source_medium' in fact_daily_kpi, parallel/non-interfering.
--
-- AI-53: firstUserSourceMedium DECLARED but not verified live (Phase B gate, AC11).

SELECT
    date,
    first_user_source_medium,
    -- Additive metric -- stored as a plain value (AD-4)
    conversions,
    pull_id,
    loaded_at,
    -- project_id propagated from raw (seeded rows -> 'default'; real pulls carry own id).
    project_id
FROM {{ source('raw_ga4', 'raw_ga4_acquisition_first_user') }}
-- AD-7 supersede semantics: when several pulls cover the same
-- (project_id, date, first_user_source_medium), the LATEST pull wins.
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY project_id, date, first_user_source_medium
    ORDER BY pull_id DESC
) = 1
