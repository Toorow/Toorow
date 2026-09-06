-- infra/nango/migrations/204_datastream_weekly_schedule.sql
--
-- AI-117 & AI-118: allow a RECURRING WEEKLY cadence on a Datastream.
-- Migration 110 constrained app.datastreams.schedule_mode to ('nightly', 'manual', 'hourly').
-- Widening the constraint to ('nightly', 'manual', 'hourly', 'weekly') allows scheduling
-- once-a-week runs while using window_days (e.g. 7 or 14 days) for lookback depth.
--
-- This migration ONLY widens the CHECK constraint. It is additive and idempotent.

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

    IF current_def IS NULL OR position('''weekly''' IN current_def) = 0 THEN
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
            CHECK (schedule_mode IN ('nightly', 'manual', 'hourly', 'weekly'));
    END IF;
END $$;

COMMIT;
