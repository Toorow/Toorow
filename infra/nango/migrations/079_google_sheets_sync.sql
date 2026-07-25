-- infra/nango/migrations/079_google_sheets_sync.sql
--
-- Story 12.10: Synchronize Google Sheets on a recurring schedule.
--
-- This migration is ADDITIVE and IDEMPOTENT (IF NOT EXISTS / ON CONFLICT DO
-- NOTHING / OR REPLACE throughout). It may be applied in any order relative to
-- other migrations -- it only ADDs columns and indexes. It does NOT alter any
-- column type, NOT drop any column, and NOT rename any column.
--
-- What this migration adds:
--
--   1. app.managed_feed_sync_schedule  -- per-datastream sync schedule config
--      for managed-feed (Google Sheets and future) sources. One row per
--      datastream; stores cadence, quota profile, and last-run state. This is
--      a separate table (not a column on app.datastreams) so the schedule can
--      be updated without touching the immutable datastream intent.
--
--   2. Indexes on managed_feed_import_ledger for the google_sheets feed_format
--      filter (used by the status/list endpoints -- additive, idempotent).
--
-- Invariants:
--   AD-2  : feed_format is OPAQUE in the app layer (no branch reads it). The
--           index here is a query performance aid only; the app never branches.
--   AD-8  : no mart table. This is Postgres metadata only.
--   NFR15 : schedule config is mutable (not append-only); the LEDGER is the
--           immutable source of truth for import history.
--   NFR20 : every sync attempt is logged in the ledger (077); this table
--           tracks the schedule configuration + last-known watermark only.
--

-- ---------------------------------------------------------------------------
-- 1. app.managed_feed_sync_schedule
-- ---------------------------------------------------------------------------
--
-- One row per managed-feed datastream. Stores:
--   datastream_id    -> FK to app.datastreams(id) (project-scoped).
--   project_id       -> FK scope (composite-scoped, same pattern as ledger).
--   connection_id    -> OAuth connection reference for the sync (passed to
--                       get_fresh_token -- NOT the token itself).
--   spreadsheet_id   -> Google Spreadsheet ID (from the Sheet URL).
--   sheet_range      -> Range string (e.g. 'Sheet1!A:F').
--   sheet_name       -> Human-readable tab name (traceability in raw).
--   column_mapping   -> Declarative mapping JSONB (date_column, metric_columns,
--                       row_id_column?) -- the 15.6 adapter contract.
--   cadence_mode     -> 'manual' | 'daily' | 'hourly'.
--   cadence_policy   -> Full schedule policy JSONB (mode, timezone,
--                       interval_minutes, watermark, missed_run, late_arrival) --
--                       consumed by core.datastream_schedule.calculate_schedule_window.
--   quota_profile    -> JSONB dict (allow_hourly, ...). Hourly cadence accepted
--                       ONLY when allow_hourly=true here.
--   last_sync_at     -> UTC timestamp of the last COMPLETED sync run (any
--                       outcome -- no_op, published, failed, rejected).
--   last_ledger_id   -> Most-recent ledger row id (mfl_<ULID>) for status display.
--   last_watermark   -> UTC timestamp of the last published window's end
--                       (passed to calculate_schedule_window as
--                       last_committed_watermark for coalesce/missed-run logic).
--   enabled          -> When FALSE the scheduler skips this datastream.
--   created_by       -> Actor who configured the sync.
--   created_at       -> Creation timestamp (immutable after insert).
--   updated_at       -> Last config-change timestamp.

CREATE TABLE IF NOT EXISTS app.managed_feed_sync_schedule (
    datastream_id       TEXT        NOT NULL,
    project_id          TEXT        NOT NULL,
    connection_id       TEXT        NOT NULL,
    spreadsheet_id      TEXT        NOT NULL,
    sheet_range         TEXT        NOT NULL,
    sheet_name          TEXT        NOT NULL DEFAULT '',
    column_mapping      JSONB       NOT NULL DEFAULT '{}',
    cadence_mode        TEXT        NOT NULL DEFAULT 'manual'
                            CHECK (cadence_mode IN ('manual', 'daily', 'hourly')),
    cadence_policy      JSONB       NOT NULL DEFAULT '{"mode":"manual"}',
    quota_profile       JSONB       NOT NULL DEFAULT '{}',
    last_sync_at        TIMESTAMPTZ NULL,
    last_ledger_id      TEXT        NULL,
    last_watermark      TIMESTAMPTZ NULL,
    enabled             BOOLEAN     NOT NULL DEFAULT TRUE,
    created_by          TEXT        NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (datastream_id, project_id),
    CONSTRAINT fk_mfss_datastream
        FOREIGN KEY (datastream_id, project_id)
        REFERENCES app.datastreams (id, project_id)
        ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED
);

COMMENT ON TABLE app.managed_feed_sync_schedule IS
    'Per-datastream sync schedule for managed-feed sources (Google Sheets, Story 12.10). '
    'Mutable (not append-only): schedule config may be changed by an authorized Member. '
    'The immutable audit history is in app.managed_feed_import_ledger (077).';

-- ---------------------------------------------------------------------------
-- 2. Indexes on managed_feed_sync_schedule
-- ---------------------------------------------------------------------------

-- Efficiently query all enabled schedules for a given cadence mode (the
-- scheduler polls: SELECT ... WHERE enabled=true AND cadence_mode='daily').
CREATE INDEX IF NOT EXISTS idx_mfss_cadence_enabled
    ON app.managed_feed_sync_schedule (cadence_mode, enabled)
    WHERE enabled = TRUE;

-- Efficiently look up by project_id (admin-api list endpoint).
CREATE INDEX IF NOT EXISTS idx_mfss_project_id
    ON app.managed_feed_sync_schedule (project_id);

-- ---------------------------------------------------------------------------
-- 3. Additive index on app.managed_feed_import_ledger for google_sheets
--    feed_format filtering (used by list/status endpoints).
-- ---------------------------------------------------------------------------
--
-- This is a partial index on the existing ledger table (from migration 077).
-- Additive + idempotent (IF NOT EXISTS). The app layer never branches on
-- feed_format (AD-2) -- this index is a query optimisation only.

CREATE INDEX IF NOT EXISTS idx_mfl_google_sheets_created
    ON app.managed_feed_import_ledger (datastream_id, created_at DESC)
    WHERE feed_format = 'google_sheets';

-- ---------------------------------------------------------------------------
-- 4. app.project_preferences: add quota_allow_hourly_sync column (additive).
-- ---------------------------------------------------------------------------
--
-- Governs hourly cadence at the project level (overrides datastream quota_profile
-- when present). Same additive pattern as max_rejected_row_pct (migration 077).

ALTER TABLE app.project_preferences
    ADD COLUMN IF NOT EXISTS quota_allow_hourly_sync BOOLEAN NULL;

COMMENT ON COLUMN app.project_preferences.quota_allow_hourly_sync IS
    'When TRUE, hourly cadence is permitted for managed-feed sync on this project. '
    'NULL = not set (falls back to datastream quota_profile.allow_hourly). '
    'Story 12.10 AC: hourly accepted only when the configured quota profile permits it.';
