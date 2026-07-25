-- infra/nango/migrations/005_create_connection_health.sql
--
-- Story 2.5: Create the connection_health table in schema app.
-- Stores the cached health state of each connection (written by the background
-- health poller; read by GET /api/connections and get_daily_report envelope).
--
-- Design decision: separate table, not columns on connection_ref.
-- Rationale: connection_ref is a governance record; health is operational and
-- write-heavy (every poll cycle). Separation prevents lock contention.
-- See Story 2.5 Dev Notes for full rationale.
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/005_create_connection_health.sql
--
-- AD-3 invariant: NO token columns. Health status only.

BEGIN;

-- Create schema if it does not exist (idempotent)
CREATE SCHEMA IF NOT EXISTS app;

-- connection_health: operational health cache per connection_ref.
-- connection_ref_id: FK to app.connection_ref.id (one health row per connection)
-- status: 'ok' | 'stale' | 'revoked' (Nango-derived classification)
-- last_checked_at: UTC timestamp when the poller last ran for this connection
-- last_fetched_at: UTC timestamp of last successful token fetch from Nango
--                  (Nango proxy for "last pull" at P2)
--                  TODO(Epic-3): replace with pull ledger timestamp
CREATE TABLE IF NOT EXISTS app.connection_health (
    connection_ref_id   TEXT        PRIMARY KEY
                            REFERENCES app.connection_ref(id) ON DELETE CASCADE,
    status              TEXT        NOT NULL
                            CHECK (status IN ('ok', 'stale', 'revoked')),
    last_checked_at     TIMESTAMPTZ NOT NULL,
    last_fetched_at     TIMESTAMPTZ
);

-- Index for status-based queries (e.g. find all stale/revoked connections)
CREATE INDEX IF NOT EXISTS idx_connection_health_status
    ON app.connection_health (status);

COMMIT;
