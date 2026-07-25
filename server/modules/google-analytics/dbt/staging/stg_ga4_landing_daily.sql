-- Staging: GA4 pages_daily_landing profile (Epic 10, story 10.3).
-- Source: raw_ga4_landing_daily (GA4 landingPage dimension -> landing_page).
-- Entry pages = sessions by landing_page. This profile carries ONLY
-- (date, landing_page, sessions) -- no country/device to normalize, so NO
-- normalize_dimension macros apply here (contrast stg_ga4_standard_daily).
-- AD-4: additive metric only (sessions).
-- AD-7: pull_id propagated from raw for the provenance chain.
--
-- TOP-N BOUNDED PARTITION (Task 1 / point dur 1): the raw rows are already
-- top-N bounded by the pull (orderBys sessions DESC + limit=page_top_n). The
-- long tail beyond page_top_n is DROPPED by design -- so the landing_page
-- partition does NOT sum to the connector daily total (unlike user_type, whose
-- 3 buckets DO sum to the total). This is correct and documented on the mart
-- block; the reconciliation test asserts the OTHER partitions are unchanged and
-- <= N distinct landing_page values per day (test_ga4_pages_reconciliation.py).
--
-- Parallel, non-interfering: this model is a NEW partition landing
-- breakdown_dimension='landing_page' in fact_daily_kpi. It does NOT touch the
-- device_category / country / user_type totals.
--
-- NO metric-integrity tests (this profile has a single additive metric, sessions),
-- only not_null + grain uniqueness (see schema.yml).

SELECT
    date,
    landing_page,
    -- Additive metric -- stored as a plain value (AD-4)
    sessions,
    pull_id,
    loaded_at,
    -- project_id propagated from raw (seeded rows -> 'default'; real pulls carry own id).
    project_id
FROM {{ source('raw_ga4', 'raw_ga4_landing_daily') }}
-- AD-7 supersede semantics: when several pulls cover the same
-- (project_id, date, landing_page), the LATEST pull wins. ULIDs are
-- lexicographically monotonic, so ORDER BY pull_id DESC = newest first.
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY project_id, date, landing_page
    ORDER BY pull_id DESC
) = 1
