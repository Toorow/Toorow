-- infra/nango/migrations/020_tenant_keys.sql
--
-- Story 7.3: Per-tenant key audit table.
--
-- Creates app.tenant_key_audit -- the audit trail for per-tenant key lifecycle
-- events (provision, rotate, delete). Every key lifecycle event writes a row
-- here so offboarding a client is provable (NFR3, FR12).
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/020_tenant_keys.sql
--
-- ID prefix: tka_ -- prefixed ULID pattern consistent with the rest of the schema.
-- FK to app.projects: ON DELETE RESTRICT so key audit rows survive an accidental
-- cascade; the archive flow sets project status='archived' (not hard delete).
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS app.tenant_key_audit (
    id              TEXT        PRIMARY KEY,           -- prefixed ULID: 'tka_'
    project_id      TEXT        NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    action          TEXT        NOT NULL               -- 'key_created', 'key_rotated', 'key_deleted'
                    CHECK (action IN ('key_created', 'key_rotated', 'key_deleted')),
    performed_by    TEXT        NOT NULL,              -- identity subject
    performed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    details         JSONB                              -- optional: {"backend": "local", "key_version": "v1"}
);
CREATE INDEX IF NOT EXISTS tenant_key_audit_project ON app.tenant_key_audit (project_id, performed_at DESC);

COMMIT;
