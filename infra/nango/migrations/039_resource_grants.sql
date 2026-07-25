-- infra/nango/migrations/039_resource_grants.sql
--
-- Story 21.5: Access resolution -- org default-closed + resource_grants (Epic 21).
--
-- This is the ACCESS-ENFORCEMENT FLIP. Migrations 035-038 built the org layer as
-- FOUNDATION ONLY (no access behaviour changed). 039 adds the fine-grained
-- allow-list table that lets a member/viewer of an org be narrowed to a subset of
-- the org's projects (or flux). It mirrors the Story 7.4 project_members pivot,
-- transposed to the member INSIDE an org:
--
--   DEFAULT-OPEN-UNTIL-ENROLLED (per human decision, Epic 21 decisions 4 & 6):
--     - An org with ZERO rows in app.org_members is OPEN (single-tenant / not yet
--       onboarded): any identity is treated as owner. This keeps 21.1-21.4 tests
--       green (their orgs have zero members, or are created via the admin API whose
--       creator is auto-enrolled as owner).
--     - An org with >= 1 member is CLOSED: only enrolled identities pass, w/ role.
--     - owner/admin see EVERYTHING in the org (downward visibility: nothing a
--       member creates is hidden upward).
--     - member/viewer with ZERO project grants -> see ALL org projects (default).
--       As soon as ONE resource_grants row of scope_type='project' exists for that
--       (org, identity), the allow-list engages: they see ONLY granted projects.
--
-- scope_id is POLYMORPHIC: it holds a project id (app.projects.id) when
-- scope_type='project' and a flux id (app.datastreams.id) when scope_type='flux'.
-- Because it targets two different parent tables, there is NO FK on scope_id --
-- existence is validated in the app layer (admin_api), exactly as the epic
-- prescribes. org_id DOES carry a FK (ON DELETE CASCADE) so revoking an org drops
-- all its grants.
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/039_resource_grants.sql
--
-- Schema-Change-Checklist (CONTRIBUTING.md):
--   [x] Additive & idempotent (IF NOT EXISTS)
--   [x] No destructive DROP/ALTER on populated columns
-- Verified: last migration is 038 (flux M:N). This is migration 039.
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- ---------------------------------------------------------------------------
-- app.resource_grants -- per-(identity, org) allow-list over projects/flux.
--   scope_id is polymorphic (project id OR flux id) validated in the app layer,
--   so there is NO FK on scope_id (documented above). org_id has a CASCADE FK.
--   capability defaults to 'view'; a viewer role caps the effective capability.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.resource_grants (
    id          TEXT        PRIMARY KEY,                -- prefixed ULID: 'rgrant_'
    org_id      TEXT        NOT NULL
                REFERENCES app.organizations(id) ON DELETE CASCADE,
    identity    TEXT        NOT NULL,                   -- opaque subject (AD-14)
    scope_type  TEXT        NOT NULL
                CHECK (scope_type IN ('project', 'flux')),
    scope_id    TEXT        NOT NULL,                   -- polymorphic: NO FK (app-validated)
    capability  TEXT        NOT NULL DEFAULT 'view'
                CHECK (capability IN ('view', 'edit', 'manage')),
    granted_by  TEXT        NOT NULL,                   -- identity subject (AD-14)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (org_id, identity, scope_type, scope_id)
);

-- Reverse lookup "which grants does this identity hold" (join-free membership probe).
CREATE INDEX IF NOT EXISTS resource_grants_identity ON app.resource_grants (identity);
-- Hot path: "does (org, identity) have ANY project grant" + "grant on THIS scope".
CREATE INDEX IF NOT EXISTS resource_grants_org_identity
    ON app.resource_grants (org_id, identity);

COMMIT;
