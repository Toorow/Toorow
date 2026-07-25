-- Staging: GA4 user_type_daily profile (Epic 10, story 10.1).
-- Source: raw_ga4_user_type_daily (GA4 newVsReturning dimension -> user_type).
-- This profile carries ONLY (date, user_type) -- no country/device to normalize,
-- so NO normalize_dimension macros apply here (contrast stg_ga4_standard_daily).
-- AD-4: additive metrics only (sessions, active_users, conversions).
-- AD-7: pull_id propagated from raw for the provenance chain.
--
-- Parallel, non-interfering: this model is a NEW partition landing
-- breakdown_dimension='user_type' in fact_daily_kpi. It does NOT touch the
-- device_category / country totals produced by stg_ga4_standard_daily (the 9.9
-- CRITICAL scenario -- reconciliation proven in
-- test_ga4_user_type_reconciliation.py).
--
-- NO metric-integrity tests (sessions_gte_active_users etc.): the new/returning
-- split at day grain does not guarantee monotonic ordering across the three
-- user_type partitions, so only not_null is enforced (see schema.yml).

SELECT
    date,
    user_type,
    -- Additive metrics -- stored as plain values (AD-4)
    sessions,
    active_users,
    conversions,
    pull_id,
    loaded_at,
    -- project_id propagated from raw (seeded rows -> 'default'; real pulls carry own id).
    project_id
FROM {{ source('raw_ga4', 'raw_ga4_user_type_daily') }}
-- AD-7 supersede semantics: when several pulls cover the same
-- (project_id, date, user_type), the LATEST pull wins. ULIDs are
-- lexicographically monotonic, so ORDER BY pull_id DESC = newest first.
QUALIFY ROW_NUMBER() OVER (
    -- project_id in the partition (review-2-7 F-5): a real-project pull covering
    -- the same dates must not supersede the seeded project's rows.
    PARTITION BY project_id, date, user_type
    ORDER BY pull_id DESC
) = 1
