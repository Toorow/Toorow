-- infra/nango/migrations/038_flux_org_scope_project_many_to_many.sql
--
-- Story 21.4: Flux org-scoped & linkable to MANY projects (Epic 21, FR37/CAP-25).
--
-- The "flux" is the physical table app.datastreams (NOT renamed -- it keeps its
-- technical identifier). Until now a flux was bound to ONE project via
-- datastreams.project_id. This migration lets a flux be OWNED by an organization
-- and LINKED to N projects of that SAME org (M:N via app.project_flux) -- so one
-- extract stream can feed several projects while never crossing the org boundary.
--
-- Structural invariant: a link row in app.project_flux carries a SHARED org_id
-- that participates in BOTH composite FKs -- one to app.projects(org_id, id) and
-- one to app.datastreams(org_id, id). Because the same org_id column feeds both,
-- it is IMPOSSIBLE to link a flux to a project of another org: project.org_id ==
-- flux.org_id == project_flux.org_id is enforced by the schema itself. The
-- per-identity "who may create/link a flux" enforcement is Story 21.5.
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/038_flux_org_scope_project_many_to_many.sql
--
-- Schema-Change-Checklist: additive & idempotent (IF NOT EXISTS / DROP CONSTRAINT
-- IF EXISTS / ON CONFLICT DO NOTHING); new column NULLable; backfill re-runnable
-- (WHERE org_id IS NULL); datastreams.project_id kept for compat (removed in a
-- later dedicated story). This is migration 038.
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- ---------------------------------------------------------------------------
-- 1. flux ownership: datastreams.org_id (+ backfill from the compat project).
--    NULLable at ALTER, then filled in this same transaction. FK RESTRICT so an
--    org owning flux cannot be hard-deleted. project_id is kept for compat.
-- ---------------------------------------------------------------------------
ALTER TABLE app.datastreams ADD COLUMN IF NOT EXISTS org_id TEXT;

-- Backfill: the flux's owner org = the org of its (compat) project.
-- Idempotent: only fills NULLs; re-run updates nothing.
UPDATE app.datastreams d
SET org_id = p.org_id
FROM app.projects p
WHERE d.project_id = p.id
  AND d.org_id IS NULL;

ALTER TABLE app.datastreams DROP CONSTRAINT IF EXISTS fk_datastreams_org;
ALTER TABLE app.datastreams
    ADD CONSTRAINT fk_datastreams_org
    FOREIGN KEY (org_id) REFERENCES app.organizations(id) ON DELETE RESTRICT;

CREATE INDEX IF NOT EXISTS idx_datastreams_org_id ON app.datastreams (org_id);

-- ---------------------------------------------------------------------------
-- 2. Composite-FK targets. A composite FK needs a UNIQUE (or PK) on the exact
--    referenced column tuple. We add UNIQUE (org_id, id) on BOTH parents so the
--    project_flux bridge can reference (org_id, project_id) and (org_id, flux_id)
--    -- the shared org_id is what forces project.org_id == flux.org_id.
-- ---------------------------------------------------------------------------
ALTER TABLE app.projects DROP CONSTRAINT IF EXISTS uq_projects_org_id;
ALTER TABLE app.projects ADD CONSTRAINT uq_projects_org_id UNIQUE (org_id, id);

ALTER TABLE app.datastreams DROP CONSTRAINT IF EXISTS uq_datastreams_org_id;
ALTER TABLE app.datastreams ADD CONSTRAINT uq_datastreams_org_id UNIQUE (org_id, id);

-- ---------------------------------------------------------------------------
-- 3. app.project_flux: the M:N bridge between projects and flux. The SHARED
--    org_id in BOTH composite FKs is the structural invariant: a link can only
--    join a project and a flux of the SAME org. ON DELETE CASCADE on both sides
--    so deleting a project or a flux drops its links.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.project_flux (
    project_id  TEXT        NOT NULL,
    flux_id     TEXT        NOT NULL,
    org_id      TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (project_id, flux_id),
    CONSTRAINT fk_project_flux_project
        FOREIGN KEY (org_id, project_id)
        REFERENCES app.projects (org_id, id)
        ON DELETE CASCADE,
    CONSTRAINT fk_project_flux_flux
        FOREIGN KEY (org_id, flux_id)
        REFERENCES app.datastreams (org_id, id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_project_flux_flux ON app.project_flux (flux_id);
CREATE INDEX IF NOT EXISTS idx_project_flux_org ON app.project_flux (org_id);

-- ---------------------------------------------------------------------------
-- 4. Backfill project_flux: one link per existing flux to its OWN project.
--    Idempotent: ON CONFLICT DO NOTHING against the PK (project_id, flux_id).
--    Only flux that already carry an org_id (step 1) are linked.
-- ---------------------------------------------------------------------------
INSERT INTO app.project_flux (project_id, flux_id, org_id)
SELECT d.project_id, d.id, d.org_id
FROM app.datastreams d
WHERE d.org_id IS NOT NULL
ON CONFLICT DO NOTHING;

COMMIT;
