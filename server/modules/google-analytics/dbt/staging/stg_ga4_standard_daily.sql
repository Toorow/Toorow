-- Staging: maps raw GA4 source fields to canonical names
-- Source fields from connector-requirements.md GA4 profile
-- AD-4: additive metrics only (sessions, active_users, conversions)
-- AD-7: pull_id propagated from raw for provenance chain
-- Seed CSV uses canonical names directly — real GA4 API fields mapped in connector.py (Story 2.7+)
-- Story 4.1 (AC4): country normalized from full name (e.g. "France") to ISO-3166 alpha-2 (e.g. "FR")
--   using normalize_dimension macro. Both raw and canonical values preserved (AD-6).
--   device_category normalized via dim_device (already canonical for GA4 values, but enforces vocabulary).
--
-- Story 4.2 (AC6) — Timezone policy (HONESTY CONSTRAINT, HG-4):
-- GA4 Standard Report dates are already bucketed in the property's reporting timezone
-- (set in GA4 property settings). At day grain, we re-label the date to the project
-- timezone when they differ; intraday precision is not available. Source date preserved
-- as 'date_source'. No actual time shift is performed at day grain.
-- Intraday re-labeling (UTC→Europe/Paris) would only matter for sessions occurring between
-- 22:00 and 00:00 UTC. Since GA4 Standard Reporting already buckets by its own timezone,
-- re-labeling at day grain means accepting the source tz boundary. We store this as a
-- policy decision and note it in dim_metric.csv's description column.
-- DO NOT add convert_timezone() at day grain — this would be misleading (HG-4).
-- Story 6.x can revisit if GA4 Realtime API (hourly data) is added.
--
-- Story 39.7 — Report-timezone CAPTURE (generic time-context contract):
-- report_timezone is passed through UNCHANGED as immutable per-row provenance (E39-AD2) —
-- the exact IANA zone the GA4 property used to draw its reporting-day boundaries
-- (time_context.locus='property'). It is CAPTURE only: it is NOT used to convert_timezone()
-- at day grain (HG-4 above stands). Undetermined at pull => NULL here (fail-closed ->
-- a read-time TIMEZONE_GAP), never a silent 'UTC'. Story 39.8 (SIGNAL half) keys on this
-- column to signal cross-source day-offsets; it never re-slices the day.

SELECT
    date AS date_source,
    date AS date,
    device_category                                                                                 AS device_category_source,
    {{ normalize_dimension('device_category', ref('dim_device'), 'aliases', 'canonical_value') }}  AS device_category,
    country                                                                                         AS country_source,
    {{ normalize_dimension('country', ref('dim_country'), 'aliases', 'iso_code') }}                AS country,
    -- Additive metrics — stored as plain values (AD-4)
    sessions,
    active_users,
    conversions,
    -- Story 39.7: immutable report-timezone provenance (CAPTURE only, no realign; HG-4).
    report_timezone,
    pull_id,
    loaded_at,
    -- Story 2.7 AC6: project_id propagated from raw to separate seed vs real data.
    -- Rows without project_id (pre-Story-2.7 seed data) get 'default' via DuckDB column default.
    project_id
FROM {{ source('raw_ga4', 'raw_ga4_standard_daily') }}
-- F-03 / AD-7 supersede semantics (Story 3.4 groundwork): when several pulls
-- cover the same (date, device_category, country), the LATEST pull wins.
-- ULIDs are lexicographically monotonic, so ORDER BY pull_id DESC = newest first.
-- AD-7 AS-OF GROUNDWORK (Story 3.4):
-- To reproduce the KPI state as-of a past date D, bypass this staging model
-- and query raw_ga4_standard_daily directly with:
--   WHERE pull_id <= 'pull_<ULID minted at cutoff date D>'
-- ULID lexicographic ordering is monotonic: pull_id <= cutoff_pull_id
-- selects only pulls that existed at date D. Story 4.6 will formalize this
-- as an as-of parameter on get_daily_report.
QUALIFY ROW_NUMBER() OVER (
    -- review-2-7 F-5: project_id in the partition -- without it, a real-project
    -- pull covering the same dates would supersede the seeded project's rows.
    PARTITION BY project_id, date, device_category, country
    ORDER BY pull_id DESC
) = 1
