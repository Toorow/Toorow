-- infra/nango/migrations/019_project_modules.sql
--
-- Story 7.2: Per-project module enablement.
--
-- Creates app.project_modules -- the per-project module on/off table -- and adds
-- an enabled column to app.connection_ref (AI-25 carry-forward from Story 3.4).
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/019_project_modules.sql
--
-- Default-enabled semantics:
--   A module NOT present in app.project_modules is treated as ENABLED by default
--   (P3-dev compatibility: existing code assumes all discovered modules are
--   available; defaulting to disabled would break all existing tests and behaviour).
--   The MODULE_DEFAULT_ENABLED env var (default "true") allows flipping to opt-in
--   for agency deployments. This decision is documented in the story Dev Notes and
--   the .env.example file.
--
-- ID prefix: pmod_ -- natural extension of the prefixed-ULID pattern.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS app.project_modules (
    id              TEXT        PRIMARY KEY,           -- prefixed ULID: 'pmod_'
    project_id      TEXT        NOT NULL REFERENCES app.projects(id) ON DELETE CASCADE,
    module_name     TEXT        NOT NULL,              -- e.g. 'google-analytics', 'meta-ads', 'gsc'
    enabled         BOOLEAN     NOT NULL DEFAULT TRUE,
    enabled_at      TIMESTAMPTZ,                       -- when last enabled (NULL if never explicitly enabled)
    disabled_at     TIMESTAMPTZ,                       -- when last disabled
    updated_by      TEXT        NOT NULL DEFAULT 'system', -- identity subject
    UNIQUE (project_id, module_name)
);
CREATE INDEX IF NOT EXISTS project_modules_project ON app.project_modules (project_id);
CREATE INDEX IF NOT EXISTS project_modules_enabled ON app.project_modules (project_id, enabled) WHERE enabled = TRUE;

-- Also add connection enabled flag (AI-25 carry-forward):
ALTER TABLE app.connection_ref
    ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT TRUE;
CREATE INDEX IF NOT EXISTS connection_ref_enabled ON app.connection_ref (project_id, enabled) WHERE enabled = TRUE;

COMMIT;
