-- infra/nango/migrations/018_projects.sql
--
-- Story 7.1: Create app.projects -- the missing anchor table -- and wire every
-- existing table that already carries a direct `project_id` column to it via a
-- foreign key. This makes the long-standing `project_id TEXT NOT NULL` columns
-- (present since migration 001) referentially meaningful.
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/018_projects.sql
--
-- ============================================================================
-- MIGRATION-SAFETY (read before running against data):
--
-- FK additions FAIL if any existing row carries a project_id value not present
-- in app.projects. This migration therefore:
--   1. Creates app.projects.
--   2. SEEDS the parent rows for every project_id value that already exists in
--      the live database BEFORE adding any FK constraint.
--   3. THEN adds the FK constraints.
-- All three steps run in ONE transaction so the whole migration is atomic.
--
-- SEED-ID DECISION (Dev Notes, AC1): existing rows carry `project_id = 'default'`
-- (a plain string, NOT a ULID). To avoid a data-rewrite of every existing row,
-- the default seed row uses `id = 'default'` (NOT 'proj_default') so it matches
-- the values already stored across the app schema. New projects created via the
-- admin API use prefixed ULIDs 'proj_<ULID>'.
--
-- The live database at implementation time also contained a second project_id
-- value 'integ-test-project' (in app.connection_ref, from Story 6.1 integration
-- fixtures). That value is ALSO seeded here so the FK on connection_ref does not
-- reject the existing row. Both seeds are idempotent (ON CONFLICT DO NOTHING).
--
-- TABLES WITH A DIRECT project_id COLUMN (verified live via information_schema):
--   alert_definitions, alert_firings, connection_ref, context_events, feedback,
--   morning_briefings, notebooks, project_preferences, project_reports  (9 tables)
--
-- INDIRECT project scoping (NO direct project_id column -- NO FK added here):
--   pull_jobs           -> scopes via connection_ref_id FK -> connection_ref.project_id
--   pull_verifications  -> scopes via connection_ref_id / pull_id
--   connection_health   -> scopes via connection_ref_id FK
--   audit_log           -> scopes via connection_ref FK
--   notebook_runs       -> scopes via notebook_id FK -> notebooks.project_id
-- These reach a project transitively; adding a project_id column + FK to them is
-- out of scope for this story (documented in the Dev Agent Record).
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- ---------------------------------------------------------------------------
-- 1. app.projects -- the anchor table.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.projects (
    id              TEXT        PRIMARY KEY,           -- prefixed ULID 'proj_' for new rows; 'default' for the legacy seed
    name            TEXT        NOT NULL,              -- human-readable name (e.g. "Acme Corp")
    slug            TEXT        NOT NULL UNIQUE,       -- URL-safe kebab-case (e.g. "acme-corp")
    status          TEXT        NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'archived')),
    currency        TEXT        NOT NULL DEFAULT 'EUR',
    timezone        TEXT        NOT NULL DEFAULT 'Europe/Paris',
    created_by      TEXT        NOT NULL,              -- identity subject from AD-14
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_at     TIMESTAMPTZ                        -- set on archive, NULL while active
);
CREATE UNIQUE INDEX IF NOT EXISTS projects_slug ON app.projects (slug);
CREATE INDEX IF NOT EXISTS projects_status ON app.projects (status);

-- ---------------------------------------------------------------------------
-- 2. Seed parent rows BEFORE adding FK constraints (migration-safety).
--    id='default' matches all existing project_id='default' values.
-- ---------------------------------------------------------------------------
INSERT INTO app.projects (id, name, slug, status, currency, timezone, created_by)
VALUES ('default', 'Default', 'default', 'active', 'EUR', 'Europe/Paris', 'system')
ON CONFLICT DO NOTHING;

-- Seed the integration-test project value discovered in the live connection_ref.
-- Without this, the connection_ref FK below would reject the existing row.
INSERT INTO app.projects (id, name, slug, status, currency, timezone, created_by)
VALUES ('integ-test-project', 'Integration Test Project', 'integ-test-project',
        'active', 'EUR', 'Europe/Paris', 'system')
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------------------
-- 3. connection_ref revocation columns (AC4).
--    connection_ref had no status/revoked_at columns (verified live). Archive +
--    connection revocation (AC4) needs them. Additive & idempotent.
--    status default 'active'; 'revoked' set on project archive.
-- ---------------------------------------------------------------------------
ALTER TABLE app.connection_ref
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'
        CONSTRAINT connection_ref_status_check CHECK (status IN ('active', 'revoked'));
ALTER TABLE app.connection_ref
    ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ;

-- ---------------------------------------------------------------------------
-- 4. FK constraints. ON DELETE RESTRICT for business tables (prevent orphans on
--    accidental project delete). ON DELETE CASCADE only for tables whose original
--    migration declared cascade-on-project semantics (notebooks 015, briefings 017).
--    Idempotency: DROP CONSTRAINT IF EXISTS before ADD so re-runs are safe (a plain
--    ADD CONSTRAINT has no IF NOT EXISTS form in PostgreSQL).
-- ---------------------------------------------------------------------------

ALTER TABLE app.connection_ref     DROP CONSTRAINT IF EXISTS fk_connection_ref_project;
ALTER TABLE app.connection_ref
    ADD CONSTRAINT fk_connection_ref_project
    FOREIGN KEY (project_id) REFERENCES app.projects(id) ON DELETE RESTRICT;

ALTER TABLE app.context_events      DROP CONSTRAINT IF EXISTS fk_context_events_project;
ALTER TABLE app.context_events
    ADD CONSTRAINT fk_context_events_project
    FOREIGN KEY (project_id) REFERENCES app.projects(id) ON DELETE RESTRICT;

ALTER TABLE app.project_preferences DROP CONSTRAINT IF EXISTS fk_project_preferences_project;
ALTER TABLE app.project_preferences
    ADD CONSTRAINT fk_project_preferences_project
    FOREIGN KEY (project_id) REFERENCES app.projects(id) ON DELETE RESTRICT;

ALTER TABLE app.alert_definitions   DROP CONSTRAINT IF EXISTS fk_alert_definitions_project;
ALTER TABLE app.alert_definitions
    ADD CONSTRAINT fk_alert_definitions_project
    FOREIGN KEY (project_id) REFERENCES app.projects(id) ON DELETE RESTRICT;

ALTER TABLE app.alert_firings       DROP CONSTRAINT IF EXISTS fk_alert_firings_project;
ALTER TABLE app.alert_firings
    ADD CONSTRAINT fk_alert_firings_project
    FOREIGN KEY (project_id) REFERENCES app.projects(id) ON DELETE RESTRICT;

ALTER TABLE app.feedback            DROP CONSTRAINT IF EXISTS fk_feedback_project;
ALTER TABLE app.feedback
    ADD CONSTRAINT fk_feedback_project
    FOREIGN KEY (project_id) REFERENCES app.projects(id) ON DELETE RESTRICT;

ALTER TABLE app.project_reports     DROP CONSTRAINT IF EXISTS fk_project_reports_project;
ALTER TABLE app.project_reports
    ADD CONSTRAINT fk_project_reports_project
    FOREIGN KEY (project_id) REFERENCES app.projects(id) ON DELETE RESTRICT;

-- notebooks: migration 015 forward-declared project cascade intent. Use CASCADE.
ALTER TABLE app.notebooks           DROP CONSTRAINT IF EXISTS fk_notebooks_project;
ALTER TABLE app.notebooks
    ADD CONSTRAINT fk_notebooks_project
    FOREIGN KEY (project_id) REFERENCES app.projects(id) ON DELETE CASCADE;

-- morning_briefings: migration 017 forward-declared project cascade intent. Use CASCADE.
ALTER TABLE app.morning_briefings   DROP CONSTRAINT IF EXISTS fk_morning_briefings_project;
ALTER TABLE app.morning_briefings
    ADD CONSTRAINT fk_morning_briefings_project
    FOREIGN KEY (project_id) REFERENCES app.projects(id) ON DELETE CASCADE;

COMMIT;
