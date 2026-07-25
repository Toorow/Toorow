-- infra/nango/migrations/006_create_pull_jobs.sql
--
-- Story 3.2: Create the pull_jobs table in schema app.
-- Stores every queued, running, and completed pull job.
-- The queue worker polls this table for 'queued' rows and transitions them
-- to 'running', 'done', 'failed', or 'dead_letter'.
--
-- Design decision: Postgres-backed job table.
-- Rationale: no new infrastructure at P3-dev; SKIP LOCKED provides
-- concurrency-safe dequeue without a separate broker. Cloud Tasks
-- (Phase B) will replace the polling loop but keep this table as the
-- status ledger so GET /api/jobs/{id} continues to work.
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/006_create_pull_jobs.sql
--

BEGIN;

-- Create schema if it does not exist (idempotent)
CREATE SCHEMA IF NOT EXISTS app;

-- pull_jobs: every pull dispatch is tracked here (AD-7 invariant).
-- id:               ULID with prefix job_
-- pull_id:          ULID with prefix pull_ (minted by enqueue_pull, AD-7)
-- connection_ref_id: FK to app.connection_ref; cascades on delete
-- date_from/date_to: the requested pull window (DATE, not TIMESTAMP)
-- state:            queued -> running -> done | failed | dead_letter
-- requested_by:     identity string from Bearer token (AD-14)
-- error_detail:     populated on failure (last error message)
-- attempt_count:    incremented on each worker attempt
-- enqueued_at:      when the job was created
-- started_at:       when the worker picked it up (first attempt)
-- completed_at:     when the job reached a terminal state
CREATE TABLE IF NOT EXISTS app.pull_jobs (
    id                TEXT        PRIMARY KEY,           -- ULID, prefix job_
    pull_id           TEXT        NOT NULL UNIQUE,       -- ULID, prefix pull_
    connection_ref_id TEXT        NOT NULL
                          REFERENCES app.connection_ref(id) ON DELETE CASCADE,
    date_from         DATE        NOT NULL,
    date_to           DATE        NOT NULL,
    state             TEXT        NOT NULL DEFAULT 'queued'
                          CHECK (state IN ('queued','running','done','failed','dead_letter')),
    requested_by      TEXT        NOT NULL,              -- identity string (AD-14)
    error_detail      TEXT,                              -- populated on failure
    attempt_count     INT         NOT NULL DEFAULT 0,
    enqueued_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at        TIMESTAMPTZ,
    completed_at      TIMESTAMPTZ
);

-- Index for state-based queue polling (worker: WHERE state = 'queued')
CREATE INDEX IF NOT EXISTS idx_pull_jobs_state
    ON app.pull_jobs (state);

-- Index for connection-scoped job listing
CREATE INDEX IF NOT EXISTS idx_pull_jobs_connection_ref
    ON app.pull_jobs (connection_ref_id);

-- Index for pull_id lookups (audit correlation, AC6)
CREATE INDEX IF NOT EXISTS idx_pull_jobs_pull_id
    ON app.pull_jobs (pull_id);

COMMIT;
