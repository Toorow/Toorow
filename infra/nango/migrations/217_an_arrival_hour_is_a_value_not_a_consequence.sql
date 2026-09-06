-- 217_an_arrival_hour_is_a_value_not_a_consequence.sql
--
-- THE HOUR A DAILY PULL IS EXPECTED TO ARRIVE GETS ITS OWN COLUMN.
-- Story 57.8, arbitrage A1. Jean, 2026-08-05: "on pull des jours ; on permet
-- juste de dire a quelle heure on veut que ca arrive, et on propose un retry
-- l'heure d'apres si le pull n'a pas marche."
--
-- WHY A COLUMN RATHER THAN THE INSTANT WE ALREADY STORE. The obvious answer is
-- that `app.datastream_schedule_state.next_run_at` already carries an hour --
-- and it does, right up to the first failure. `_advance_next_run`
-- (`server/core/scheduler.py`) advances from the PREVIOUS value, so the moment a
-- catch-up writes `next_run_at = NOW() + 1 hour` that off-hour instant becomes
-- the anchor for every following day. The chosen hour is lost by the mechanism
-- that exists to protect it. An hour a person named has to be a VALUE the
-- advance reads, not a consequence the advance inherits.
--
-- WHY ON `app.datastreams` AND NOT ON THE PLAN VERSION. The other candidate was
-- `plan_intent.schedule.watermark.delay_minutes`, which already reaches
-- `calculate_schedule_window` end to end. It cannot host an editable setting:
-- `app.datastream_plan_versions` is immutable by trigger (migration 030,
-- `RAISE EXCEPTION 'datastream plan versions are immutable'`), so the Workbench
-- could never change the hour it displays. The plan version keeps recording what
-- was reviewed at activation; this column carries what is in force now.
--
-- SMALLINT, closed range, NULLABLE. NULL means "no arrival hour named", which
-- resolves to local midnight -- the default an activated row has always had
-- (`delay_minutes = 0` against a local-midnight boundary). NULL is therefore not
-- an absence of behaviour, it is the absence of a CHOICE, and the console says
-- exactly that rather than rendering a 0.

ALTER TABLE app.datastreams
    ADD COLUMN IF NOT EXISTS arrival_hour_local SMALLINT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_datastreams_arrival_hour_local'
    ) THEN
        ALTER TABLE app.datastreams
            ADD CONSTRAINT ck_datastreams_arrival_hour_local
            CHECK (arrival_hour_local IS NULL
                   OR (arrival_hour_local >= 0 AND arrival_hour_local <= 23));
    END IF;
END $$;

COMMENT ON COLUMN app.datastreams.arrival_hour_local IS
    'Story 57.8: hour of the project-local day a daily/weekly pull is expected to '
    'arrive (0-23). NULL means no hour was chosen and the run lands at local '
    'midnight. Read by scheduler._advance_next_run and by schedule_mcp.read_schedule.';

-- BACKFILL: every row that is already anchored keeps its anchor.
--
-- Without this, the first advance after this migration would move a schedule
-- someone deliberately set to 02:00 down to local midnight -- the exact silent
-- displacement A1 exists to stop, committed by the change meant to prevent it.
-- Reading the hour out of the instant that is in force turns the consequence
-- into the value, once, for rows that already have one.
--
-- The 'Europe/Paris' fallback is COPIED, deliberately and only here, from
-- `scheduler.project_timezone` (`SCHEDULER_TIMEZONE`, default Europe/Paris): SQL
-- cannot read the deployment's environment, and a backfill that used a different
-- zone than the dispatcher would record an hour the dispatcher never ran at.
-- It is a one-shot read of a constant, never a second source of truth.
UPDATE app.datastreams d
   SET arrival_hour_local = EXTRACT(
           HOUR FROM (
               ss.next_run_at AT TIME ZONE COALESCE(pp.reporting_timezone, 'Europe/Paris')
           )
       )::smallint
  FROM app.datastream_schedule_state ss
  LEFT JOIN app.project_preferences pp ON pp.project_id = ss.project_id
 WHERE ss.datastream_id = d.id
   AND ss.project_id = d.project_id
   AND ss.plan_version_id = d.current_plan_version_id
   AND ss.next_run_at IS NOT NULL
   AND d.arrival_hour_local IS NULL
   AND d.schedule_mode IN ('nightly', 'weekly');
