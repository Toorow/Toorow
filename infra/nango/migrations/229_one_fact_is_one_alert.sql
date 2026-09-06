-- ============================================================================
-- 229: one fact is one alert.
--
-- WHY THIS EXISTS. `013_alert_firings_project_metric.sql:16-21` gave two firing
-- kinds a partial unique index -- "a persistent breach must not spam a new row
-- every night" -- and `044_mediaplan_alert_prefs.sql:66-68` gave a third one.
-- The six DQ kinds never got one, and they are the kinds that fire the most.
--
-- Measured 2026-08-08:
--
--     app.alert_firings by type   dq_timeliness 2421 · meta_alert 1
--                                 nango_revoke_failed 1
--     distinct datastreams        590
--     actual_status               never_fetched on 2421 of 2421
--     window_date                 2026-08-04 on 2421 of 2421
--
-- 2421 rows for 590 facts. And the amplifier is not a bug anywhere: the clock
-- `toorow-run-dq-monitors` fires every fifteen minutes by design (AI-113 -- the
-- monitors had NO push trigger and `--min-instances=0` left the in-process loop
-- nowhere to run), and `DQ_TIMELINESS_DUE_HOUR` opens the gate at 09:00. From
-- nine to midnight that is sixty ticks, and a Datastream still missing yesterday
-- is still missing it at every one of them. Each tick re-stated the same fact.
--
-- WHY IT COULD NOT BE FIXED WITH THE EXISTING COLUMNS. The dedup key for a DQ
-- finding is (project, kind, WHICH DATASTREAM, which day). `metric` carries the
-- check name -- 'timeliness' for every Datastream of a project -- so keying on it
-- the way `anomaly` does would collapse a whole project into one alert and hide
-- 589 real findings to suppress 1831 duplicates. The subject was never a column:
-- it travelled inside `metadata`, concatenated into `message`
-- ("alert_firings has no separate metadata column at P3-dev"). This migration
-- gives the subject a column, which is what makes the key expressible.
--
-- THE INDEX IS PARTIAL, twice over. `WHERE type LIKE 'dq\_%'` because an infra
-- event (meta_alert, nango_revoke_failed, scheduler_step_degraded) has no
-- Datastream and no daily identity -- deduplicating those is a different question
-- and this migration does not answer it. `AND datastream_id IS NOT NULL` because
-- a DQ firing whose subject could not be resolved must still be written: an alert
-- refused for want of provenance is an alert deleted by its own metadata.
--
-- BACKFILL: none, deliberately. The 2423 existing rows are not rewritten -- they
-- are the measurement this repair is justified by, and a unique index created
-- CONCURRENTLY-less over rows that violate it would fail the migration. The
-- column is NULL on every historical row, which the partial index excludes.
--
-- Schema-Change-Checklist (CONTRIBUTING.md):
--   [x] Additive & idempotent (IF NOT EXISTS everywhere; replayable)
--   [x] New column is NULL-able, no default, no rewrite of the table
--   [x] No destructive DROP/ALTER on populated columns
--   [x] The new index cannot fail on existing rows: they are all NULL here
-- ============================================================================

BEGIN;

ALTER TABLE app.alert_firings
    ADD COLUMN IF NOT EXISTS datastream_id TEXT;

COMMENT ON COLUMN app.alert_firings.datastream_id IS
    'WHICH Datastream this finding is about, for the firing kinds that have one. '
    'NULL for infra events, and NULL on every row written before migration 229 -- '
    'their subject survives only inside the concatenated message. Deliberately '
    'not a foreign key: a firing outlives the Datastream it accuses, and an alert '
    'that vanished when its subject was deleted would erase the only trace that '
    'anything was ever wrong with it.';

-- One finding, one day, one Datastream, one alert. The tick may re-observe the
-- same fact ninety-six times a day; it states it once.
CREATE UNIQUE INDEX IF NOT EXISTS alert_firings_dedup_dq
    ON app.alert_firings (project_id, type, datastream_id, window_date)
    WHERE type LIKE 'dq\_%' AND datastream_id IS NOT NULL;

COMMIT;
