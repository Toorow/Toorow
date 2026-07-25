-- infra/nango/migrations/043_gsc_full_coverage.sql
--
-- GSC full Search Analytics API coverage (searchanalytics.query).
--
-- NUMBERING (AI-53, code over story): drafted as 041, renumbered to 043 — a
-- parallel session landed 041_media_plan_mappings.sql and
-- 042_datastream_candidate_registry.sql first.
--
-- Adds three nullable columns to the raw GSC landing table so the connector can
-- land every reporting surface and dimension the API exposes:
--
--   search_type       — the API 'type' parameter (web / image / video / news /
--                       discover / googleNews). NULL on legacy rows == 'web';
--                       the staging models treat NULL and 'web' identically.
--                       Discover and Google News data are ONLY reachable through
--                       this parameter — without it those surfaces are invisible.
--   search_appearance — the searchAppearance dimension (rich results, AMP, ...).
--                       Only the search_appearance_daily profile lands it; the API
--                       forbids grouping searchAppearance with other dimensions,
--                       so the connector loops one single-day query per date.
--   hour              — the hour dimension (ISO-8601 timestamp keys, requires
--                       dataState=hourly_all). Ad-hoc pulls only; excluded from
--                       all daily staging models (partial data by definition).
--
-- ADDITIVE and NULLABLE (Schema Change Checklist AI-29): existing rows keep NULLs
-- and are naturally routed by the staging filters; no destructive ALTER, no
-- default required. Companion of migration 027 (query column) — the connector's
-- _RAW_ADD_COLUMN_DDLS applies the same guards on connector-created DuckDB tables.
--
-- Apply with:
--   psql $PLATFORM_DB_URL -f infra/nango/migrations/043_gsc_full_coverage.sql

ALTER TABLE raw_gsc_daily ADD COLUMN IF NOT EXISTS search_type VARCHAR;
ALTER TABLE raw_gsc_daily ADD COLUMN IF NOT EXISTS search_appearance VARCHAR;
ALTER TABLE raw_gsc_daily ADD COLUMN IF NOT EXISTS hour VARCHAR;
