-- 218_a_running_collection_writes_where_it_is.sql
--
-- A RUN THAT IS COLLECTING WRITES WHERE IT IS. Story 63.1, epic 63.
--
-- Measured 2026-08-05 on preprod: `app.datastream_executions` held 0 rows for 6
-- `app.pull_jobs`. The two halves of a collection were never joined -- the paths
-- that collect (nightly dispatch, hourly dispatch, refetch) create N pull jobs
-- and ZERO executions, while the only path that creates an execution enqueues no
-- pull at all. Adding progress columns alone would have shipped an epic whose
-- screen stays empty for 100% of the traffic, so this migration carries BOTH
-- halves of the join: the progress columns, and the `execution_id` that binds a
-- queued window to the run it belongs to.
--
-- WHY COLUMNS AND NOT AN APPEND-ONLY TABLE. Progress is a CURRENT STATE, not a
-- history: no screen asks what every window did, only where the run is now. An
-- append-only table would cost the RGPD erasure hatch (migration 200) plus an
-- `org_purge` edge, for a monotone value. `app.datastream_executions` already
-- carries one row per run, is mutable, has no trigger (042 says so in its own
-- header), and is the table the epic-63 surfaces already read. The UPDATE is
-- idempotent: GREATEST() never lets a replayed window move a counter backwards.
--
-- WHY `started_at` IS ADDED AND NOT DERIVED. `state_changed_at` moves on EVERY
-- state transition, so a "when did this run start" read off it would answer
-- "when did it last change", which is a different question and a wrong answer.
-- Story 57.8 paid for exactly that class of defect one layer above. A dedicated
-- column costs one line of DDL.
--
-- WHY `progress_updated_at` IS SEPARATE FROM `updated_at`. Same reason, same
-- class: `updated_at` is bumped by every write to the row, including state
-- transitions that observed nothing. The screen has to be able to say WHEN the
-- number it displays was measured; a timestamp that also moves for unrelated
-- reasons cannot say it.

-- ---------------------------------------------------------------------------
-- 1. Progress columns on the execution row.
-- ---------------------------------------------------------------------------

ALTER TABLE app.datastream_executions
    ADD COLUMN IF NOT EXISTS step                TEXT,
    ADD COLUMN IF NOT EXISTS day_in_progress     DATE,
    ADD COLUMN IF NOT EXISTS days_done           INTEGER,
    ADD COLUMN IF NOT EXISTS days_total          INTEGER,
    ADD COLUMN IF NOT EXISTS rows_written        BIGINT,
    ADD COLUMN IF NOT EXISTS started_at          TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS progress_updated_at TIMESTAMPTZ;

-- The step vocabulary is the product's own, ratified in
-- docs/product-architecture/datastream-workbench-and-wizard.md: `Collect`,
-- `Map`, `Check`, `Publish`, aligned on the data stages collected / mapped /
-- processed / published. Never `Fetch`, `Enrich` or `Load`.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_datastream_executions_step'
    ) THEN
        ALTER TABLE app.datastream_executions
            ADD CONSTRAINT ck_datastream_executions_step
            CHECK (step IS NULL OR step IN ('Collect', 'Map', 'Check', 'Publish'));
    END IF;
END $$;

-- The bounds the story names, under the name the story names, so a violation
-- reads as a sentence rather than as a random constraint id. NULL is allowed
-- everywhere: an execution that has written no progress is NOT a zero, and
-- `days_total` stays NULL when the run declared no window (never 0 -- a
-- fabricated total is the most expensive defect in the product).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_datastream_executions_progress_bounds'
    ) THEN
        ALTER TABLE app.datastream_executions
            ADD CONSTRAINT ck_datastream_executions_progress_bounds
            CHECK (
                (days_done IS NULL OR days_done >= 0)
                AND (rows_written IS NULL OR rows_written >= 0)
                AND (days_done IS NULL OR days_total IS NULL OR days_done <= days_total)
            );
    END IF;
END $$;

COMMENT ON COLUMN app.datastream_executions.step IS
    'Story 63.1: the ratified step this run is on -- Collect / Map / Check / Publish.';
COMMENT ON COLUMN app.datastream_executions.day_in_progress IS
    'Story 63.1: the first day of the window currently being collected, or the '
    'last day reached once every window is terminal. A DATE, never an hour.';
COMMENT ON COLUMN app.datastream_executions.days_done IS
    'Story 63.1: days covered by the FINISHED windows of this run, derived from '
    'their bounds. The unit written is the window; the day count is what the '
    'screen reads. Monotone.';
COMMENT ON COLUMN app.datastream_executions.days_total IS
    'Story 63.1: days covered by every window this run declared. NULL when no '
    'window was declared -- never 0.';
COMMENT ON COLUMN app.datastream_executions.rows_written IS
    'Story 63.1: rows landed by the finished windows of this run. Advances at a '
    'window boundary only, never per page or per row. Monotone.';
COMMENT ON COLUMN app.datastream_executions.started_at IS
    'Story 63.1: when this run began moving data. Dedicated column: '
    'state_changed_at moves on every transition and cannot answer this.';
COMMENT ON COLUMN app.datastream_executions.progress_updated_at IS
    'Story 63.1: when the progress numbers above were last measured, so the '
    'screen can say how old they are. Distinct from updated_at, which moves for '
    'reasons that observed nothing.';

-- ---------------------------------------------------------------------------
-- 2. A terminal state for a run that collected and published nothing.
-- ---------------------------------------------------------------------------
--
-- The 042 machine is created -> loading -> validating -> ready -> publishing ->
-- published, with failed / cancelled reachable from any non-terminal state.
-- A recurring collection run passes through none of the publication states: it
-- pulls its windows and stops, because nothing downstream is armed to map, check
-- or publish them automatically. Of the three terminal states, `published` is a
-- lie the output pointers would read, `failed` is a lie and `cancelled` is a
-- lie -- and leaving the run non-terminal is worse than all three: the partial
-- unique index `uq_datastream_executions_active` allows ONE non-terminal
-- execution per datastream, so a collection run that never terminates would make
-- every later publish -- and every following night's dispatch -- answer 409
-- forever.
--
-- `collected` is therefore added as a FOURTH TERMINAL state. It is deliberately
-- NOT added to `uq_datastream_executions_active`'s predicate (which enumerates
-- the five active states explicitly and so already excludes it) and NOT added to
-- `datastream_publication.ACTIVE_STATES`: a collected run blocks nothing.
DO $$
BEGIN
    ALTER TABLE app.datastream_executions
        DROP CONSTRAINT IF EXISTS datastream_executions_state_check;
    ALTER TABLE app.datastream_executions
        ADD CONSTRAINT datastream_executions_state_check
        CHECK (state IN (
            'created', 'loading', 'validating', 'ready',
            'publishing', 'published', 'collected', 'failed', 'cancelled'
        ));
END $$;

-- ---------------------------------------------------------------------------
-- 3. The queue learns which run its window belongs to, and what it landed.
-- ---------------------------------------------------------------------------
--
-- `app.pull_jobs` knew WHICH WINDOW ran and SINCE WHEN, and carried no execution
-- identity at all -- which is why the progress of a collection could not be
-- written anywhere a screen reads. `execution_id` is the join.
--
-- ON DELETE SET NULL, deliberately, and no composite scope: pull_jobs is the
-- queue's internal recovery registry and holds neither org_id nor project_id of
-- its own (it is scoped through connection_ref). A RESTRICT here would make the
-- queue able to block an org erasure, which no recovery registry should.
--
-- `row_count` is what the window landed, recorded at the SAME boundary the job
-- is marked done. It exists so `rows_written` can be DERIVED from the finished
-- windows rather than accumulated: a derived sum is idempotent under replay,
-- an accumulator is not.
ALTER TABLE app.pull_jobs
    ADD COLUMN IF NOT EXISTS execution_id TEXT,
    ADD COLUMN IF NOT EXISTS row_count    BIGINT;

ALTER TABLE app.pull_jobs
    DROP CONSTRAINT IF EXISTS fk_pull_jobs_execution;
ALTER TABLE app.pull_jobs
    ADD CONSTRAINT fk_pull_jobs_execution
    FOREIGN KEY (execution_id)
    REFERENCES app.datastream_executions (id) ON DELETE SET NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_pull_jobs_row_count'
    ) THEN
        ALTER TABLE app.pull_jobs
            ADD CONSTRAINT ck_pull_jobs_row_count
            CHECK (row_count IS NULL OR row_count >= 0);
    END IF;
END $$;

-- The progress read is "every job of this execution": one index, that shape.
CREATE INDEX IF NOT EXISTS idx_pull_jobs_execution
    ON app.pull_jobs (execution_id)
    WHERE execution_id IS NOT NULL;

COMMENT ON COLUMN app.pull_jobs.execution_id IS
    'Story 63.1: the app.datastream_executions run this window belongs to. NULL '
    'for a window enqueued outside a run (legacy per-connection dispatch).';
COMMENT ON COLUMN app.pull_jobs.row_count IS
    'Story 63.1: rows this window landed, written when the job is marked done. '
    'Lets execution progress be derived rather than accumulated.';
