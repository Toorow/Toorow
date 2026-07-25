-- infra/nango/migrations/002_create_audit_log.sql
--
-- Story 2.6: Create the audit_log table in schema app.
-- Migration tooling: raw SQL (matching 001_ convention).
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/002_create_audit_log.sql
--
-- AD-8 invariant: app.audit_log is Postgres-owned. BigQuery never writes to it.
-- AD-14 invariant: every row carries the identity (subject) of who triggered the action.
-- AD-3 invariant: NO token columns. Identity is a subject string, never a credential.
-- Append-only: UPDATE and DELETE are REVOKED from the application role (connector).

BEGIN;

-- audit_log: one row per auditable action performed on behalf of a user.
-- id: ULID, prefixed audit_ (populated by application layer, not DB)
-- identity: the subject from the access token (AD-14); "anonymous" in disabled mode
-- action: event code, e.g. connection.created / connection.revoked / pull.triggered
-- provider_account: Nango provider_config_key (e.g. 'google-analytics')
-- connection_ref: FK to app.connection_ref(id) -- the conn_ ULID of the connection
-- metadata: optional free-form JSONB context (e.g. trigger source, pull batch id)
-- created_at: UTC timestamp, immutable (append-only table, no updated_at)
CREATE TABLE IF NOT EXISTS app.audit_log (
    id               TEXT        PRIMARY KEY,          -- ULID, prefix audit_
    identity         TEXT        NOT NULL,             -- subject from token (AD-14)
    action           TEXT        NOT NULL,             -- e.g. connection.created
    provider_account TEXT        NOT NULL,             -- Nango provider key
    connection_ref   TEXT        NOT NULL REFERENCES app.connection_ref(id),
    metadata         JSONB,                            -- optional free-form context
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Index for filtering by identity (AD-14 query pattern)
CREATE INDEX IF NOT EXISTS idx_audit_log_identity
    ON app.audit_log (identity);

-- Index for filtering by action code
CREATE INDEX IF NOT EXISTS idx_audit_log_action
    ON app.audit_log (action);

-- Index for filtering by connection
CREATE INDEX IF NOT EXISTS idx_audit_log_connection_ref
    ON app.audit_log (connection_ref);

-- Index for time-ordered queries (DESC is the read pattern)
CREATE INDEX IF NOT EXISTS idx_audit_log_created_at
    ON app.audit_log (created_at DESC);

-- Enforce append-only at the DB level (AC3).
-- The application role retains INSERT and SELECT; UPDATE and DELETE are removed.
-- This is the canonical enforcement layer -- application-layer guards are secondary.
REVOKE UPDATE, DELETE ON app.audit_log FROM connector;

COMMIT;
