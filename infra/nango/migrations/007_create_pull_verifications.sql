-- infra/nango/migrations/007_create_pull_verifications.sql
--
-- Story 3.5: Create pull_verifications table and extend connection_health status enum.
--
-- pull_verifications: post-pull completeness audit record (AD-13 populate verification).
-- Every successful pull is followed by a verification that checks the rows actually
-- landed against the expected coverage window. Empty or sparse pulls produce a
-- 'populate_failed' connection_health status (distinct from 'revoked' / 'stale').
--
-- This migration also extends connection_health.status to include 'populate_failed'
-- (HG-5: both changes in one file so the schema is consistent after a single run).
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/007_create_pull_verifications.sql
--

BEGIN;

-- Create schema if it does not exist (idempotent)
CREATE SCHEMA IF NOT EXISTS app;

-- pull_verifications: one row per pull, written by the queue worker post-pull hook.
-- id:                 ULID with prefix ver_
-- pull_id:            FK to app.pull_jobs.pull_id (UNIQUE: one verification per pull)
-- connection_ref_id:  FK to app.connection_ref.id
-- expected_rows:      rows expected based on manifest report profile and date window
-- actual_rows:        rows counted in the raw table for this pull_id
-- completeness_ratio: actual / expected, clamped 0.0000-1.0000; 1.0 when expected=0
-- verdict:            'ok' | 'partial' | 'empty' (see VERIFICATION_PARTIAL_THRESHOLD)
-- verified_at:        UTC timestamp when the verification ran
CREATE TABLE IF NOT EXISTS app.pull_verifications (
    id                TEXT        PRIMARY KEY,           -- ULID, prefix ver_
    pull_id           TEXT        NOT NULL UNIQUE
                          REFERENCES app.pull_jobs(pull_id) ON DELETE CASCADE,
    connection_ref_id TEXT        NOT NULL
                          REFERENCES app.connection_ref(id) ON DELETE CASCADE,
    expected_rows     INT         NOT NULL,
    actual_rows       INT         NOT NULL,
    completeness_ratio NUMERIC(5,4) NOT NULL,           -- actual/expected, 0.0000-1.0000
    verdict           TEXT        NOT NULL
                          CHECK (verdict IN ('ok','partial','empty')),
    verified_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Index for pull_id lookups (primary query pattern: GET /api/jobs/{id}/verification)
CREATE INDEX IF NOT EXISTS idx_pull_verifications_pull_id
    ON app.pull_verifications (pull_id);

-- Index for connection-scoped verification history
CREATE INDEX IF NOT EXISTS idx_pull_verifications_connection_ref
    ON app.pull_verifications (connection_ref_id);

-- Extend connection_health.status to include 'populate_failed'.
-- PostgreSQL does not allow adding values to a CHECK constraint in-place;
-- the constraint must be dropped and recreated (HG-5).
-- Original constraint from migration 005: status IN ('ok', 'stale', 'revoked')
ALTER TABLE app.connection_health
    DROP CONSTRAINT IF EXISTS connection_health_status_check;
ALTER TABLE app.connection_health
    ADD CONSTRAINT connection_health_status_check
        CHECK (status IN ('ok', 'stale', 'revoked', 'populate_failed'));

COMMIT;
