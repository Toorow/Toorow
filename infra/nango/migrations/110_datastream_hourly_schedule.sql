-- infra/nango/migrations/110_datastream_hourly_schedule.sql
--
-- Story 12.6 (Phase-B debt close): allow a RECURRING HOURLY cadence on a
-- Datastream. Migration 023 constrained app.datastreams.schedule_mode to
-- ('nightly', 'manual'); the versioned wizard (12.13) produces a cadence in
-- {manual, daily, hourly} where daily maps to the nightly dispatch and hourly
-- needs its own recurring dispatch loop (scheduler._dispatch_hourly_datastreams).
--
-- This migration ONLY widens the CHECK to admit 'hourly'. It is additive: every
-- existing row (nightly/manual) stays valid; the wider set is a superset. No new
-- column, no data change. Idempotent (the DO block only swaps the constraint when
-- the widened form is not already present). Mirrors migration 080's approach.

BEGIN;

DO $$
DECLARE
    current_def TEXT;
BEGIN
    SELECT pg_get_constraintdef(c.oid)
      INTO current_def
      FROM pg_constraint c
      JOIN pg_class t ON t.oid = c.conrelid
      JOIN pg_namespace n ON n.oid = t.relnamespace
     WHERE n.nspname = 'app'
       AND t.relname = 'datastreams'
       AND c.conname = 'datastreams_schedule_mode_check';

    IF current_def IS NULL OR position('''hourly''' IN current_def) = 0 THEN
        IF EXISTS (
            SELECT 1 FROM pg_constraint c
              JOIN pg_class t ON t.oid = c.conrelid
              JOIN pg_namespace n ON n.oid = t.relnamespace
             WHERE n.nspname = 'app'
               AND t.relname = 'datastreams'
               AND c.conname = 'datastreams_schedule_mode_check'
        ) THEN
            ALTER TABLE app.datastreams
                DROP CONSTRAINT datastreams_schedule_mode_check;
        END IF;

        ALTER TABLE app.datastreams
            ADD CONSTRAINT datastreams_schedule_mode_check
            CHECK (schedule_mode IN ('nightly', 'manual', 'hourly'));
    END IF;
END $$;

COMMIT;
