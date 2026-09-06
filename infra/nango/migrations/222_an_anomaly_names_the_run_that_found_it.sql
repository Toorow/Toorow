-- 222_an_anomaly_names_the_run_that_found_it.sql
--
-- AN ANOMALY UNFOLDS INSIDE ITS RUN, SO IT HAS TO BE ABLE TO NAME ONE. Story
-- 58.10, epic 58; the amendment of 2026-08-05 (n°5) reads "une anomalie se
-- deplie dans le run qui l'a trouvee -- un badge sur la liste mene au run,
-- jamais a une fenetre flottante".
--
-- MEASURED 2026-08-07, and this is the whole reason. `app.dq_issues`
-- (`145_controls_and_quality.sql:514-537`) carries `project_id` and
-- `monitor_id`, and NOTHING ELSE that locates the finding: no
-- `datastream_id`, no `execution_id`. `app.dq_evaluations` (`:449-496`) is the
-- same. So the ratified amendment describes a navigation the schema cannot
-- express -- not a missing screen, a missing foreign key. Every design of the
-- Runs track needs this column before it can render one row, which is why it
-- lands before the story rather than inside it.
--
-- BOTH COLUMNS ARE NULLABLE, AND THAT IS THE POINT. A monitor may be
-- project-scoped rather than bound to one Datastream, and an evaluation may run
-- on a schedule rather than on the back of a collection. A NOT NULL here would
-- have forced a fabricated Datastream onto every project-wide check -- the
-- absence is a real answer and the read model must say it, not fill it.
--
-- WHY THE COMPOSITE FOREIGN KEYS. Same shape as
-- `app.datastream_execution_phase_evidence` (`138:79-82`): the pair carries
-- `project_id` so a row cannot point at a Datastream in another Project. With
-- MATCH SIMPLE -- Postgres' default -- a NULL in any column of the key skips the
-- check entirely, which is exactly the behaviour a project-scoped issue needs.
-- `ON DELETE RESTRICT` matches its siblings: evidence is never silently dropped
-- under a run. Organization erasure still works, because `project_id` already
-- cascades from `app.projects` (`145:517`, `:452`) and the purge goes through
-- the Project.
--
-- NO BACKFILL, AND NOTHING TO BACKFILL. `SELECT count(*) FROM app.dq_issues` and
-- `app.dq_evaluations` -> 0 on preprod AND on the disposable base, measured
-- today. There is no historical row whose run could be guessed, and guessing one
-- is how a screen starts showing an anomaly under a collection that never
-- produced it.

-- ---------------------------------------------------------------------------
-- 1. An issue names the Datastream and the run it was found on.
-- ---------------------------------------------------------------------------

ALTER TABLE app.dq_issues
    ADD COLUMN IF NOT EXISTS datastream_id TEXT,
    ADD COLUMN IF NOT EXISTS execution_id  TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_dq_issues_datastream'
    ) THEN
        ALTER TABLE app.dq_issues
            ADD CONSTRAINT fk_dq_issues_datastream
            FOREIGN KEY (datastream_id, project_id)
            REFERENCES app.datastreams(id, project_id) ON DELETE RESTRICT;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_dq_issues_execution'
    ) THEN
        ALTER TABLE app.dq_issues
            ADD CONSTRAINT fk_dq_issues_execution
            FOREIGN KEY (execution_id, datastream_id, project_id)
            REFERENCES app.datastream_executions(id, datastream_id, project_id)
            ON DELETE RESTRICT;
    END IF;

    -- A run without its Datastream is not a weaker statement, it is an
    -- unreadable one: the composite key above would be skipped outright.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_dq_issues_execution_needs_datastream'
    ) THEN
        ALTER TABLE app.dq_issues
            ADD CONSTRAINT ck_dq_issues_execution_needs_datastream
            CHECK (execution_id IS NULL OR datastream_id IS NOT NULL);
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. An evaluation names them too -- an issue is a conclusion drawn from these.
-- ---------------------------------------------------------------------------

ALTER TABLE app.dq_evaluations
    ADD COLUMN IF NOT EXISTS datastream_id TEXT,
    ADD COLUMN IF NOT EXISTS execution_id  TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_dq_evaluations_datastream'
    ) THEN
        ALTER TABLE app.dq_evaluations
            ADD CONSTRAINT fk_dq_evaluations_datastream
            FOREIGN KEY (datastream_id, project_id)
            REFERENCES app.datastreams(id, project_id) ON DELETE RESTRICT;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_dq_evaluations_execution'
    ) THEN
        ALTER TABLE app.dq_evaluations
            ADD CONSTRAINT fk_dq_evaluations_execution
            FOREIGN KEY (execution_id, datastream_id, project_id)
            REFERENCES app.datastream_executions(id, datastream_id, project_id)
            ON DELETE RESTRICT;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_dq_evaluations_execution_needs_datastream'
    ) THEN
        ALTER TABLE app.dq_evaluations
            ADD CONSTRAINT ck_dq_evaluations_execution_needs_datastream
            CHECK (execution_id IS NULL OR datastream_id IS NOT NULL);
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 3. The read this exists for: every anomaly of ONE run, in one index hit.
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_dq_issues_execution
    ON app.dq_issues (project_id, datastream_id, execution_id)
    WHERE execution_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_dq_evaluations_execution
    ON app.dq_evaluations (project_id, datastream_id, execution_id)
    WHERE execution_id IS NOT NULL;

COMMENT ON COLUMN app.dq_issues.execution_id IS
'Story 58.10: the run this anomaly was found on. NULL for a project-scoped check that no collection produced.';

COMMENT ON COLUMN app.dq_evaluations.execution_id IS
'Story 58.10: the run this evaluation read. NULL for a scheduled evaluation that rode no collection.';
