-- infra/nango/migrations/021_project_members.sql
--
-- Story 7.4: Identity -> project membership table for per-identity project ACL.
--
-- At P3-dev the admin API uses a single shared Bearer token (API_SECRET_KEY):
-- any holder is "the admin" and reaches every project. This table establishes
-- the enforcement point for true per-identity scoping (AD-5, FR12) WITHOUT
-- breaking the single-tenant default.
--
-- DEFAULT-OPEN FALLBACK (single-tenant compat):
--   When app.project_members is EMPTY, every identity is granted access to every
--   project (the current behaviour). Enforcement only engages once at least one
--   membership row exists for a project: from that point, only enrolled
--   identities reach that project. This keeps all existing tests green (empty
--   table => existing behaviour) while making the ACL real the moment an agency
--   deployment starts enrolling identities.
--
--   The check is per-project: a project with zero membership rows stays open;
--   a project with >=1 row is closed to non-members. This lets a multi-tenant
--   deployment onboard project-by-project without a big-bang cutover.
--
-- ID prefix: pmem_ -- natural extension of the prefixed-ULID pattern
-- (proj_, pmod_, conn_, nb_, ...).
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/021_project_members.sql
--
-- Additive & idempotent per the CONTRIBUTING Schema Change Checklist.
-- Verified: last migration was 020 (Story 7.3). This is 021.
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS app.project_members (
    id          TEXT        PRIMARY KEY,                    -- prefixed ULID: 'pmem_'
    project_id  TEXT        NOT NULL REFERENCES app.projects(id) ON DELETE CASCADE,
    identity    TEXT        NOT NULL,                       -- subject string from the access token (AD-14)
    role        TEXT        NOT NULL DEFAULT 'member'
                CHECK (role IN ('owner', 'member', 'viewer')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, identity)
);

-- Fast membership lookup by (project_id, identity) is served by the UNIQUE
-- constraint's implicit index. Add a reverse index for "which projects can this
-- identity see" queries.
CREATE INDEX IF NOT EXISTS project_members_identity ON app.project_members (identity);
-- Partial-scoping guard: fast "does this project have ANY member rows" probe
-- (drives the per-project default-open decision without a full scan).
CREATE INDEX IF NOT EXISTS project_members_project ON app.project_members (project_id);

-- ---------------------------------------------------------------------------
-- audit_log.connection_ref: allow NULL for non-connection audit actions.
--
-- Story 7.4 writes ACTION_CROSS_SCOPE_ATTEMPT (access_denied) audit rows that
-- are NOT tied to a Nango connection (they refuse a notebook/report access).
-- Migration 002 declared connection_ref NOT NULL REFERENCES app.connection_ref;
-- an empty-string sentinel would VIOLATE that FK (epic-5-retro L-1: mocks masked
-- this — no live-Postgres audit test existed). This relaxes the column to allow
-- NULL so connection-agnostic audit actions (project_created, key_rotated,
-- access_denied, ...) record cleanly. Existing connection.* rows are unchanged.
-- The FK still holds for non-NULL values.
-- ---------------------------------------------------------------------------
ALTER TABLE app.audit_log ALTER COLUMN connection_ref DROP NOT NULL;

COMMIT;
