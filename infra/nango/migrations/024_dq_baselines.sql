-- infra/nango/migrations/024_dq_baselines.sql
--
-- Story 8.6: Quality monitors v1 (Epic 8, Part A -- Monitors).
--
-- Creates:
--   app.dq_baselines   -- schema-consistency baseline per datastream (column set)
--
-- Alters:
--   app.alert_firings  -- adds acknowledged_at TIMESTAMPTZ NULL (DQ issue ack)
--                         and message TEXT NULL (already present from 011 as TEXT,
--                         adding IF NOT EXISTS is safe/no-op on existing installs)
--
-- project_id / metric columns on alert_firings already added by migration 013.
-- message column already present in migration 011's INSERT; guarded with IF NOT EXISTS.
--
-- Idempotency: IF NOT EXISTS / ADD COLUMN IF NOT EXISTS throughout. Safe to re-run.
-- Additive: no destructive DROP/ALTER on existing data.
--
-- AD-2: no module or provider names hardcoded.
-- Schema-Change-Checklist (CONTRIBUTING.md AI-29):
--   [x] Migration is additive and idempotent
--   [x] New columns are NULL-able or have defaults
--   [x] IF NOT EXISTS used throughout
--   [x] No destructive DROP/ALTER on populated columns
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- ---------------------------------------------------------------------------
-- 1. app.dq_baselines
--
-- Stores the "known-good" column set for each datastream's raw table.
-- First run of the schema-consistency monitor seeds a row here.
-- On drift detected, the monitor fires once then AUTO-RESETS this row
-- (baseline advances after alerting).
--
-- column_set: JSONB array of column name strings, sorted ASC for stable comparison.
-- updated_at: timestamp of last baseline write (seed or auto-reset).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.dq_baselines (
    datastream_id  TEXT        NOT NULL
                   REFERENCES app.datastreams(id) ON DELETE CASCADE,
    column_set     JSONB       NOT NULL DEFAULT '[]',
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_dq_baselines PRIMARY KEY (datastream_id)
);

CREATE INDEX IF NOT EXISTS idx_dq_baselines_datastream_id
    ON app.dq_baselines (datastream_id);

-- ---------------------------------------------------------------------------
-- 2. ALTER app.alert_firings -- add acknowledged_at (nullable TIMESTAMPTZ).
--
-- Stores the timestamp when a DQ firing was acknowledged via the API.
-- NULL = unacknowledged. Additive: pre-existing rows stay NULL (unacknowledged).
-- project_id, metric already added by migration 013.
-- message already added by migration 011 CREATE TABLE (TEXT column, no NOT NULL).
-- ---------------------------------------------------------------------------
ALTER TABLE app.alert_firings
    ADD COLUMN IF NOT EXISTS acknowledged_at TIMESTAMPTZ NULL;

-- Index for efficient "list unacknowledged DQ firings per project" queries.
CREATE INDEX IF NOT EXISTS idx_alert_firings_dq_type
    ON app.alert_firings (project_id, type, fired_at DESC)
    WHERE type LIKE 'dq_%';

CREATE INDEX IF NOT EXISTS idx_alert_firings_ack
    ON app.alert_firings (acknowledged_at)
    WHERE acknowledged_at IS NULL;

COMMIT;
