-- infra/nango/migrations/035_organizations.sql
--
-- Story 21.1: Organizations & membership foundation (Epic 21, FR37/CAP-25).
--
-- Introduces the ORGANIZATION layer above the project. Until now the isolation
-- unit was the project (app.projects + app.project_members, Epic 7). An agency
-- managing several clients needs an organization above the project, and a user
-- who belongs to 1..N organizations.
--
-- FOUNDATION ONLY -- this migration creates tables and backfills; it changes NO
-- access behaviour. The default-open fallback of Story 7.4 stays active; the
-- org-level default-closed flip is Story 21.5. Existing isolation tests stay
-- green without modification.
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/035_organizations.sql
--
-- ID prefixes: 'org_' (organizations), 'omem_' (org_members) -- natural
-- extension of the prefixed-ULID pattern (proj_, pmem_, conn_, ...).
--
-- Schema-Change-Checklist (CONTRIBUTING.md):
--   [x] Additive & idempotent (IF NOT EXISTS / ON CONFLICT DO NOTHING)
--   [x] New columns are NULL-able or have defaults (projects.org_id NULLable then backfilled)
--   [x] No destructive DROP/ALTER on populated columns
-- Verified: last real migration is 034 (query_adherence). This is 035.
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- ---------------------------------------------------------------------------
-- 1. app.organizations -- the tenant (the isolation boundary of Epic 21).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.organizations (
    id              TEXT        PRIMARY KEY,           -- prefixed ULID: 'org_'
    name            TEXT        NOT NULL,
    slug            TEXT        NOT NULL UNIQUE,        -- URL-safe kebab-case
    status          TEXT        NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'archived')),
    billing_ref     TEXT,                              -- non-goal: monetisation
    created_by      TEXT        NOT NULL,              -- identity subject (AD-14)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_at     TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS organizations_slug ON app.organizations (slug);
CREATE INDEX IF NOT EXISTS organizations_status ON app.organizations (status);

-- updated_at trigger (reuse app.set_updated_at() from migration 023).
DROP TRIGGER IF EXISTS trg_organizations_updated_at ON app.organizations;
CREATE TRIGGER trg_organizations_updated_at
    BEFORE UPDATE ON app.organizations
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- ---------------------------------------------------------------------------
-- 2. app.org_members -- identity <-> org <-> role. A user in N orgs = N rows.
--    Role has 4 levels (adds 'admin' vs project_members' 3): admin manages
--    members and exposes auth (per-account grants land in Story 21.3).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.org_members (
    id          TEXT        PRIMARY KEY,               -- prefixed ULID: 'omem_'
    org_id      TEXT        NOT NULL REFERENCES app.organizations(id) ON DELETE CASCADE,
    identity    TEXT        NOT NULL,                  -- opaque subject (AD-14)
    role        TEXT        NOT NULL DEFAULT 'member'
                CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
    status      TEXT        NOT NULL DEFAULT 'active'
                CHECK (status IN ('invited', 'active', 'suspended')),
    invited_by  TEXT,
    invited_at  TIMESTAMPTZ,
    joined_at   TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (org_id, identity)
);
CREATE INDEX IF NOT EXISTS org_members_identity ON app.org_members (identity);
CREATE INDEX IF NOT EXISTS org_members_org ON app.org_members (org_id);

-- ---------------------------------------------------------------------------
-- 3. app.projects.org_id + backfill (one organization per existing project).
--    NULLable at ALTER, then filled in this same transaction. NOT NULL is
--    deferred to Story 21.5 (once no project can be born without an org).
-- ---------------------------------------------------------------------------
ALTER TABLE app.projects ADD COLUMN IF NOT EXISTS org_id TEXT;

-- Backfill parent org rows. id is DERIVED from the project ('org_' || p.id) so
-- re-running the migration is a no-op (idempotent). This derived scheme is for
-- the backfill only; orgs created via the admin API use a fresh 'org_<ULID>'.
-- review-stack F-HIGH: gate the INSERT to projects WITHOUT an org so we never
-- fabricate a phantom org for a project that already carries a (real) org_id.
-- review-stack F-HIGH: the org slug is 'org-' || p.slug (NOT p.slug) so the
-- backfill can never collide with an admin-created org that took p.slug --
-- organizations.slug is UNIQUE and ON CONFLICT (id) would NOT catch a slug clash.
INSERT INTO app.organizations (id, name, slug, status, created_by)
SELECT 'org_' || p.id, p.name, 'org-' || p.slug, 'active', 'system'
FROM app.projects p
WHERE p.org_id IS NULL
ON CONFLICT (id) DO NOTHING;

UPDATE app.projects p
SET org_id = 'org_' || p.id
WHERE p.org_id IS NULL;

-- Seed org membership from existing project members (role owner by default).
-- review-stack F-MEDIUM: JOIN app.projects to use the project's ACTUAL org_id
-- (set by the UPDATE above) rather than re-deriving 'org_'||project_id -- this
-- respects a project that was pre-assigned a non-derived org. Untargeted
-- ON CONFLICT DO NOTHING keeps a re-run a no-op against BOTH unique axes (PK id
-- and composite UNIQUE (org_id, identity)).
INSERT INTO app.org_members (id, org_id, identity, role, status, joined_at)
SELECT 'omem_' || pm.id, p.org_id, pm.identity, 'owner', 'active', NOW()
FROM app.project_members pm
JOIN app.projects p ON p.id = pm.project_id
ON CONFLICT DO NOTHING;

-- FK last (all rows now carry a valid org_id). RESTRICT so an org cannot be
-- hard-deleted while it still owns projects.
ALTER TABLE app.projects DROP CONSTRAINT IF EXISTS fk_projects_org;
ALTER TABLE app.projects
    ADD CONSTRAINT fk_projects_org
    FOREIGN KEY (org_id) REFERENCES app.organizations(id) ON DELETE RESTRICT;

CREATE INDEX IF NOT EXISTS projects_org ON app.projects (org_id);

COMMIT;
