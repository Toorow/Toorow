-- infra/nango/migrations/086_tracked_entity_registry.sql
--
-- Story 40.1: ORG-scoped tracked-brand entity master + PROJECT-scoped role.
--
-- The DATA-MODEL FOUNDATION of Epic 40 (competitor / brand registry). It SPECIALISES
-- Epic 27's conformed-dimension layer (Story 27.4, app.dimension_value_mappings) -- it
-- does NOT refork it. It promotes the "tracked brand" conformed dimension to a
-- first-class master-data ENTITY at the ORG anchor and adds a PROJECT-scoped ROLE on top.
-- Two tables and nothing more:
--
--   1. app.tracked_entities      -- the ORG entity master (E40-FR01): one canonical
--      record per brand per org (canonical_name + aliases), reusable by every project.
--   2. app.entity_project_roles  -- the PROJECT-scoped role (E40-FR02): one row per
--      (entity, project) giving the project's view (own|competitor|reference), behind a
--      HARD confidentiality boundary (E40-NFR01) -- a project sees ONLY its own role
--      assignments and can never enumerate a sibling project's tracked brands.
--
-- ENTITY = ORG, ROLE = PROJECT (E40-AD1): the entity is anchored at the ORG (one master
-- record per org); the role is a FLAT PROJECT fact (one row per (entity, project)), NOT a
-- PLATFORM>ORG>PROJECT cascade row. The client's OWN brand is an ordinary tracked_entity
-- carrying an 'own' role -- a first-class entity, not a special case.
--
-- SCOPING: tracked_entities is ORG-scoped ONLY. We keep a scope_level column with a
-- stricter CHECK (scope_level = 'ORG') -- rather than dropping it -- so the table is
-- visibly part of the same scope family as 049/052, and a future PLATFORM seed
-- (Phase B auto-seeding, a Non-goal here) becomes a CHECK relaxation, not a schema fork.
-- org_id is NOT NULL (there is no PLATFORM or PROJECT entity in v1). The scope-triplet
-- CHECK is repeated in SQL (NOT imported -- SQL cannot import); the Python validate_scope
-- (server/core/metric_semantics.py) is the shared mirror used by the store + fake tests.
--
-- CROSS-ORG INTEGRITY: a role links an ORG-scoped entity to a project of the SAME org
-- (entity.org_id == project.org_id). This is enforced by a Python store guard
-- (server/core/tracked_entity_registry.set_entity_project_role -> CrossProjectDenied),
-- NOT a SQL trigger: the store is the single write path in v1 (documented in the story
-- Completion Notes). The FKs below guarantee referential integrity; the org-equality
-- guard lives in the store.
--
-- STRICTLY ADDITIVE & PASSIVE (40.1): this migration CREATES the two tables + indexes +
-- triggers only; NOTHING is wired onto the runtime. No warehouse row and no existing join
-- is read or written (E40-NFR06 satisfied by inaction). Per-source binding is 40.2;
-- inbound alias matching + the governed new-entity alert (Epic 13) is 40.3; the MCP
-- governance surface is 40.5.
--
-- AUDIT: NO new audit table. The append-only registry app.metric_semantics_audit
-- (migration 049, ALREADY APPLIED to Supabase) has a FREE entity_type TEXT column (no
-- enum CHECK). 40.1 writes there with entity_type in {'tracked_entity',
-- 'entity_project_role'} via the shared _write_semantics_audit helper. The append-only
-- enforcement (REVOKE UPDATE/DELETE) already lives on that table from 049 -- 086 adds
-- nothing there. This migration therefore has a SOFT dependency on 049 (its audit table
-- must exist); 049 is already in prod, so 086 does NOT recreate it.
--
-- ID prefixes (prefixed-ULID pattern): 'tent_' (tracked_entities), 'epr_'
-- (entity_project_roles). Audit rows keep 049's 'msaudit_' prefix (shared table).
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/086_tracked_entity_registry.sql
--
-- Schema-Change-Checklist (CONTRIBUTING.md):
--   [x] Additive & idempotent (IF NOT EXISTS everywhere; the whole file is replayable)
--   [x] New columns are NULL-able or have defaults
--   [x] No destructive DROP/ALTER on populated columns
-- Verified at dev time: the highest migration present is 085 (connector_verification_runs,
-- Story 38.4); 085 was TAKEN by a parallel session, so 40.1 takes the next free number
-- 086 (noted in the story Completion Notes).
--
-- PENDING -- NOT applied to Supabase (human gate: explicit go from Jean, like 052/046/048
-- before their go). The orchestrator may apply it to a throwaway test DB. 049 is ALREADY
-- applied in prod, so its audit table exists and 086 does NOT recreate it.
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- ---------------------------------------------------------------------------
-- app.tracked_entities -- the ORG entity master (E40-FR01).
--
-- ORG-scoped ONLY: scope_level is always 'ORG' (one master record per org), org_id is
-- NOT NULL. Aliases are a flat JSONB array of strings in v1 (matching + negative aliases
-- + provenance-per-alias are 40.3; hierarchy / M&A versioning is Phase B -- NOT modelled
-- here). status defaults to 'approved' because in v1 an org admin authors entities
-- directly; the governed draft->approved path (Epic 13) is engaged by 40.3's new-entity
-- alert -- the column exists now so 40.3 needs no migration.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.tracked_entities (
    id             TEXT        PRIMARY KEY,                 -- prefixed ULID: 'tent_'
    org_id         TEXT        NOT NULL REFERENCES app.organizations(id) ON DELETE CASCADE,
    scope_level    TEXT        NOT NULL DEFAULT 'ORG' CHECK (scope_level = 'ORG'),
    canonical_name TEXT        NOT NULL,                    -- the one canonical brand label (e.g. 'Peugeot')
    display_name   TEXT,                                    -- optional human label (NULL -> canonical_name)
    aliases        JSONB       NOT NULL DEFAULT '[]'::jsonb, -- ordered list of known alias strings (flat, v1)
    entity_kind    TEXT        NOT NULL DEFAULT 'brand'
                   CHECK (entity_kind IN ('brand')),         -- v1: flat brand; 'product'/'org_group' reserved Phase B (hierarchy)
    -- provenance / governance seam (Epic 13 reused in 40.3/40.5; v1 defaults to 'approved'
    -- for an org-admin-authored entity -- the governed draft->approved alert is 40.3):
    status         TEXT        NOT NULL DEFAULT 'approved'
                   CHECK (status IN ('draft','approved','archived')),
    created_by     TEXT        NOT NULL,                    -- identity subject (AD-14)
    approved_by    TEXT,                                    -- who approved (NULL while 'draft')
    approved_at    TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- UNICITY: ONE canonical entity per org per canonical_name (case-preserving; a case-
-- insensitive alias match is 40.3's job, not a DB constraint here). The org owns exactly
-- one 'Peugeot'. No COALESCE needed (org_id is NOT NULL) but kept in the same family form.
CREATE UNIQUE INDEX IF NOT EXISTS uq_tracked_entities_org_name
    ON app.tracked_entities (org_id, canonical_name);

-- Resolution / listing by org (the confidentiality-safe enumeration surface is per-org).
CREATE INDEX IF NOT EXISTS ix_tracked_entities_org
    ON app.tracked_entities (org_id, status);

-- GIN on aliases for the future alias lookup (40.3); harmless additive index in v1.
CREATE INDEX IF NOT EXISTS ix_tracked_entities_aliases
    ON app.tracked_entities USING gin (aliases jsonb_path_ops);

DROP TRIGGER IF EXISTS trg_tracked_entities_updated_at ON app.tracked_entities;
CREATE TRIGGER trg_tracked_entities_updated_at
    BEFORE UPDATE ON app.tracked_entities
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

COMMENT ON TABLE app.tracked_entities IS
    'Story 40.1: ORG-scoped tracked-brand master entity (canonical_name + aliases), declared once per org and reused by every project (E40-FR01, E40-AD1). Specialises the 27.4 conformed-dimension layer: the "tracked brand" dimension promoted to a first-class entity. Per-source binding is 40.2; inbound alias matching + governed new-entity alert (Epic 13) is 40.3. Mutations audited in app.metric_semantics_audit (049) with entity_type=tracked_entity.';

-- ---------------------------------------------------------------------------
-- app.entity_project_roles -- the PROJECT-scoped role (E40-FR02).
--
-- A PROJECT fact, not a scoped-triplet row: it links an ORG-scoped entity to a project.
-- It carries project_id NOT NULL and derives its org from the entity/project (no
-- scope_level column -- it is not a cascade row). The same entity is 'own' in project P
-- and 'competitor' in project Q -- two DIFFERENT rows, distinct project_id.
--
-- CONFIDENTIALITY BY CONSTRUCTION (E40-NFR01): the ONLY per-project listing surface
-- (ix_entity_project_roles_project) is keyed on project_id; there is no store function
-- that, given project P, returns project Q's roled entities. The ix_..._entity reverse
-- index exists for an ORG-INTERNAL owner/admin view (built in 40.5 behind the org guard),
-- never for a sibling-project read.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.entity_project_roles (
    id          TEXT        PRIMARY KEY,                    -- prefixed ULID: 'epr_'
    entity_id   TEXT        NOT NULL REFERENCES app.tracked_entities(id) ON DELETE CASCADE,
    project_id  TEXT        NOT NULL REFERENCES app.projects(id)         ON DELETE CASCADE,
    role        TEXT        NOT NULL
                CHECK (role IN ('own','competitor','reference')),
    created_by  TEXT        NOT NULL,                       -- identity subject (AD-14)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- UNICITY: ONE role per (entity, project). The same entity is 'own' in project P and
-- 'competitor' in project Q -- two DIFFERENT rows, distinct project_id. Re-roling a pair
-- UPSERTs the existing row (role change, one audit).
CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_project_roles_entity_project
    ON app.entity_project_roles (entity_id, project_id);

-- The confidentiality-safe listing surface: a project's OWN tracked brands only.
CREATE INDEX IF NOT EXISTS ix_entity_project_roles_project
    ON app.entity_project_roles (project_id, role);

-- Reverse lookup ('which projects role this entity') is ORG-INTERNAL only -- exposed by
-- no confidentiality-crossing store function; the index serves owner/admin org views (40.5).
CREATE INDEX IF NOT EXISTS ix_entity_project_roles_entity
    ON app.entity_project_roles (entity_id);

DROP TRIGGER IF EXISTS trg_entity_project_roles_updated_at ON app.entity_project_roles;
CREATE TRIGGER trg_entity_project_roles_updated_at
    BEFORE UPDATE ON app.entity_project_roles
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

COMMENT ON TABLE app.entity_project_roles IS
    'Story 40.1: PROJECT-scoped role (own|competitor|reference) assigned to an ORG-scoped tracked_entity (E40-FR02, E40-AD1). The same entity is own in one project and competitor in another (distinct rows). CONFIDENTIALITY BOUNDARY (E40-NFR01): a project sees ONLY its own rows; there is no cross-project enumerator. The clients OWN brand is an ordinary entity carrying an own role (first-class). Mutations audited in app.metric_semantics_audit (049) with entity_type=entity_project_role.';

COMMIT;
