-- Staging: GA4 acquisition_daily_session profile (Epic 16, story 16.1).
-- Source: raw_ga4_acquisition_session (GA4 sessionSourceMedium x sessionCampaignName,
-- last-click axis). Carries (date, session_source_medium, session_campaign,
-- conversions, sessions) -- no country/device to normalize, so NO normalize_dimension
-- macros apply here (contrast stg_ga4_standard_daily).
-- AD-4: additive metrics only (conversions, sessions).
-- AD-7: pull_id propagated from raw for the provenance chain.
--
-- TWO MARGINAL PARTITIONS (Task 1): the mart derives breakdown_dimension
-- 'session_source_medium' AND 'session_campaign' from this single-grain staging row
-- (like country/device_category are two marginals of the standard row). This is NOT a
-- cross-join between attribution axes -- the forbidden cross-join is session x first-user.
--
-- TOP-N BOUNDED PARTITION (point dur 1): raw rows are already top-N bounded by the pull
-- (orderBys conversions DESC + limit=top_n). The long tail is DROPPED by design -- so
-- these partitions do NOT sum to the connector daily total (unlike user_type). Correct
-- and documented on the mart; the reconciliation test asserts the OTHER partitions are
-- unchanged and <= N distinct values per day (test_ga4_acquisition_reconciliation.py).
--
-- AI-53: sessionSourceMedium / sessionCampaignName DECLARED but not verified live
-- (Phase B gate, AC11). Parallel, non-interfering with the other GA4 staging models.

SELECT
    date,
    session_source_medium,
    session_campaign,
    -- Additive metrics -- stored as plain values (AD-4)
    conversions,
    sessions,
    pull_id,
    loaded_at,
    -- project_id propagated from raw (seeded rows -> 'default'; real pulls carry own id).
    project_id
FROM {{ source('raw_ga4', 'raw_ga4_acquisition_session') }}
-- AD-7 supersede semantics: when several pulls cover the same
-- (project_id, date, session_source_medium, session_campaign), the LATEST pull wins.
-- ULIDs are lexicographically monotonic, so ORDER BY pull_id DESC = newest first.
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY project_id, date, session_source_medium, session_campaign
    ORDER BY pull_id DESC
) = 1
