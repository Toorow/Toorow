-- infra/nango/migrations/001_create_connection_ref.sql
--
-- Story 2.2: Create the connection_ref table in schema app.
-- Migration tooling: raw SQL (chosen over Alembic for this story — see Story 2.2 Dev Notes).
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/001_create_connection_ref.sql
--
-- AD-3 invariant: NO token columns. Tokens live only in Nango.

BEGIN;

-- Create schema if it does not exist
CREATE SCHEMA IF NOT EXISTS app;

-- connection_ref: a pointer from our platform to a Nango connection.
-- id: ULID, prefixed conn_ (populated by application layer, not DB)
-- nango_connection_id: the identifier Nango uses for this connection
-- project_id: references future project hierarchy (no FK constraint yet — Story 2.6)
-- AD-3: no access_token, refresh_token, or any OAuth credential column here.
CREATE TABLE IF NOT EXISTS app.connection_ref (
    id                  TEXT        PRIMARY KEY,            -- ULID, prefix conn_
    provider            TEXT        NOT NULL,               -- e.g. 'google-analytics'
    nango_connection_id TEXT        NOT NULL,               -- Nango connection identifier
    project_id          TEXT        NOT NULL,               -- future project hierarchy
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Index for fast lookup by project
CREATE INDEX IF NOT EXISTS idx_connection_ref_project_id
    ON app.connection_ref (project_id);

-- Index for fast lookup by provider
CREATE INDEX IF NOT EXISTS idx_connection_ref_provider
    ON app.connection_ref (provider);

COMMIT;
