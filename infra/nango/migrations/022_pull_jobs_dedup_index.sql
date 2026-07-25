-- infra/nango/migrations/022_pull_jobs_dedup_index.sql
--
-- G-12 / enqueue-dedup-race fix: add a named partial UNIQUE constraint on
-- app.pull_jobs so that concurrent enqueue_pull() calls for the same
-- (connection_ref_id, date_from, date_to) with an active state
-- ('queued' or 'running') are serialised by Postgres rather than by a
-- SELECT-then-INSERT race in application code.
--
-- The INSERT in LocalBackend.enqueue_pull() uses:
--   ON CONFLICT (connection_ref_id, date_from, date_to)
--       WHERE state IN ('queued', 'running') DO NOTHING
-- Postgres infers this partial unique index from the column list + predicate.
-- The loser atomically detects the conflict and fetches the winning row.
--
-- Defensive pre-step: before creating the index we eliminate any duplicate
-- active rows that may have been inserted by the old SELECT-then-INSERT path.
-- We keep the NEWEST row per (connection_ref_id, date_from, date_to, state)
-- group (highest ULID = lexicographically largest id) and mark the older
-- duplicates as 'superseded' with an explanatory error_detail.  This ensures
-- the unique constraint can be created without violating existing data.
--
-- 'superseded' is NOT in the existing CHECK constraint; we widen it below.
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector \
--     -f /migrations/022_pull_jobs_dedup_index.sql
--

BEGIN;

-- Step 1: widen the state CHECK to include 'superseded' so the UPDATE below
-- does not violate the constraint.
ALTER TABLE app.pull_jobs
    DROP CONSTRAINT IF EXISTS pull_jobs_state_check;

ALTER TABLE app.pull_jobs
    ADD CONSTRAINT pull_jobs_state_check
    CHECK (state IN ('queued','running','done','failed','dead_letter','superseded'));

-- Step 2: de-duplicate existing active rows, keeping the newest per group.
-- Older duplicates are moved to 'superseded' (terminal, not retried).
UPDATE app.pull_jobs AS pj
SET state        = 'superseded',
    error_detail = 'dedup_migration: superseded by newer active job for same window',
    completed_at = now()
WHERE state IN ('queued', 'running')
  AND id NOT IN (
      SELECT DISTINCT ON (connection_ref_id, date_from, date_to)
             id
      FROM   app.pull_jobs
      WHERE  state IN ('queued', 'running')
      ORDER  BY connection_ref_id, date_from, date_to, id DESC   -- newest ULID wins
  );

-- Step 3: create the partial unique index.  IF NOT EXISTS makes the migration
-- idempotent.  The application code references this index via its name in the
-- ON CONFLICT (connection_ref_id, date_from, date_to) WHERE ... clause; we
-- rely on Postgres inferring the index from the column list + predicate.
-- Dropping the index before re-creating avoids "already exists" errors when
-- the migration is re-applied after a partial run.
-- Note: the index lives in the app schema (same as its table); DROP INDEX must
-- be schema-qualified or it would silently look in the search_path only.
DROP INDEX IF EXISTS app.uq_pull_jobs_active;

CREATE UNIQUE INDEX uq_pull_jobs_active
    ON app.pull_jobs (connection_ref_id, date_from, date_to)
    WHERE state IN ('queued', 'running');

COMMIT;
