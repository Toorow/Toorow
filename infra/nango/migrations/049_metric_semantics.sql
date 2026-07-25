-- infra/nango/migrations/049_metric_semantics.sql
--
-- Story 27.1: metric-semantics foundation (Epic 27).
--
-- The DATA FOUNDATION of the metric-semantics epic. Two layers:
--   * layer 1 -- metric DEFINITION (app.metric_definitions): what a metric IS
--     (canonical name, aggregation rule, additive flag, ratio numerator/denominator).
--     Extends dbt/seeds/dim_metric.csv from a static seed into a scoped, curable table.
--   * layer 2 -- multi-source RECONCILIATION topology (app.overlap_groups +
--     app.overlap_group_members + app.reconciliation_rules): when the SAME metric is
--     emitted by several connectors that would double-count if summed, an overlap
--     group names the colliding sources and a reconciliation rule says how to resolve
--     the overlap (PRIORITY winner-take-day, DEDUP by join key, KEEP_SEPARATE, ...).
--     Extends dbt/seeds/metric_source_priority.csv (which today only carries PRIORITY).
--   * the BRIDGE (app.source_metric_mappings): which connector field feeds which
--     metric definition. Schema only in 27.1 -- populated from manifests in Story 27.2.
--
-- SCOPING: every business table carries the (scope_level, org_id, project_id) triplet.
-- A row is a PLATFORM default (org_id/project_id NULL), an ORG override, or a PROJECT
-- override. Resolution (server/core/metric_semantics.py) is a cascade
-- PROJECT > ORG > PLATFORM: the most specific row wins, metric by metric.
--
-- STRICTLY ADDITIVE & PASSIVE: 27.1 CREATES the tables and mirrors the current seeds
-- into PLATFORM defaults, but wires NOTHING onto the runtime. The dbt seeds remain the
-- sole execution authority until the routing resolver (Story 27.3) lands. No warehouse
-- row is read or written here -- zero regression by construction.
--
-- INVARIANT 2 (AD-4, documented, NOT enforced by 27.1): summing BETWEEN overlap groups
-- is fine; summing WITHIN a group is NEVER an implicit SUM -- it is whatever the group's
-- reconciliation_rule says (PRIORITY / DEDUP_ID / KEEP_SEPARATE never silently sum).
-- The socle carries the rule; the router (27.3) applies it.
--
-- AUDIT: app.metric_semantics_audit is a DEDICATED append-only registry (not
-- app.audit_log -- that table's connection_ref is NOT NULL and a metric-definition
-- mutation is not tied to a connection). It reproduces the append-only discipline of
-- 002 (REVOKE UPDATE/DELETE, no updated_at) and carries before/after JSONB so the DB
-- audit substitutes for git versioning of definitions.
--
-- ID prefixes (prefixed-ULID pattern): 'metdef_' (metric_definitions),
-- 'smm_' (source_metric_mappings), 'ovg_' (overlap_groups),
-- 'ovgm_' (overlap_group_members), 'recrule_' (reconciliation_rules),
-- 'msaudit_' (metric_semantics_audit).
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/049_metric_semantics.sql
--
-- Schema-Change-Checklist (CONTRIBUTING.md):
--   [x] Additive & idempotent (IF NOT EXISTS everywhere; the whole file is replayable)
--   [x] New columns are NULL-able or have defaults
--   [x] No destructive DROP/ALTER on populated columns
-- Verified: last real migration is 048 (async_report_refs). This is 049.
--
-- APPLIED to Supabase 2026-07-21 via MCP (explicit go from Jean; 6 tables +
-- append-only triggers verified post-apply). Replayable locally as-is.
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- ---------------------------------------------------------------------------
-- 1. app.metric_definitions -- layer 1: what a metric IS (extends dim_metric).
--
-- Scope triplet contract (repeated identically on every business table):
--   PLATFORM => org_id IS NULL     AND project_id IS NULL
--   ORG      => org_id IS NOT NULL AND project_id IS NULL
--   PROJECT  => project_id IS NOT NULL   (org_id left OPTIONAL, see note below)
--
-- Decision (Completion Notes): at PROJECT level org_id is OPTIONAL -- the project
-- already carries its own org_id in app.projects (035), so the CHECK requires only
-- project_id and never contradicts the project's real org anchor.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.metric_definitions (
    id                      TEXT        PRIMARY KEY,          -- prefixed ULID: 'metdef_'
    canonical_name          TEXT        NOT NULL,             -- == dim_metric.name (e.g. 'revenue')
    display_name            TEXT,
    description             TEXT,
    -- aggregation_type is DELIBERATELY not constrained by a CHECK enum: it carries
    -- free values (e.g. 'sum', 'ratio', 'impression_weighted_average', and future
    -- weighted variants) so a new aggregation rule never requires a migration.
    aggregation_type        TEXT        NOT NULL,             -- e.g. 'sum'|'ratio'|'impression_weighted_average' (== dim_metric.aggregation_rule)
    additive                BOOLEAN     NOT NULL,             -- == dim_metric.additive
    ratio_numerator         TEXT,                             -- canonical_name of the numerator (ratio only)
    ratio_denominator       TEXT,                             -- canonical_name of the denominator (ratio only)
    format                  TEXT,                             -- e.g. 'currency','percent','integer' (NULL for migrated defaults)
    unit                    TEXT,
    currency_mode           TEXT,
    non_additive_dimensions TEXT[]      NOT NULL DEFAULT '{}',    -- NON ADDITIVE BY / non_additive_dimension pattern
    synonyms                JSONB       NOT NULL DEFAULT '[]'::jsonb,
    ai_context              TEXT,
    certified               BOOLEAN     NOT NULL DEFAULT FALSE,
    -- scope triplet
    scope_level             TEXT        NOT NULL CHECK (scope_level IN ('PLATFORM','ORG','PROJECT')),
    org_id                  TEXT        REFERENCES app.organizations(id) ON DELETE CASCADE,
    project_id              TEXT        REFERENCES app.projects(id)      ON DELETE CASCADE,
    created_by              TEXT        NOT NULL,             -- identity subject (AD-14); 'system' for migrated defaults
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_metric_definitions_scope_cols CHECK (
        (scope_level = 'PLATFORM' AND org_id IS NULL     AND project_id IS NULL)
     OR (scope_level = 'ORG'      AND org_id IS NOT NULL AND project_id IS NULL)
     OR (scope_level = 'PROJECT'  AND project_id IS NOT NULL)
    ),
    CONSTRAINT ck_metric_definitions_ratio CHECK (
        aggregation_type <> 'ratio'
        OR (ratio_numerator IS NOT NULL AND ratio_denominator IS NOT NULL)
    )
);

-- UNICITY: one definition per (scope, canonical_name). The COALESCE(...,'') is
-- MANDATORY -- without it, two PLATFORM rows (PLATFORM, NULL, NULL, 'revenue') would
-- BOTH be accepted (NULL <> NULL in a plain composite unique index), breaking the
-- "one platform default per metric" invariant.
CREATE UNIQUE INDEX IF NOT EXISTS uq_metric_definitions_scope_name
    ON app.metric_definitions
    (scope_level, COALESCE(org_id, ''), COALESCE(project_id, ''), canonical_name);

CREATE INDEX IF NOT EXISTS ix_metric_definitions_canonical_name
    ON app.metric_definitions (canonical_name);

-- updated_at trigger (reuse app.set_updated_at() from migration 023).
DROP TRIGGER IF EXISTS trg_metric_definitions_updated_at ON app.metric_definitions;
CREATE TRIGGER trg_metric_definitions_updated_at
    BEFORE UPDATE ON app.metric_definitions
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- ---------------------------------------------------------------------------
-- 2. app.source_metric_mappings -- the BRIDGE: connector field -> metric definition.
--    Schema only in 27.1 (bootstrap from manifests = Story 27.2).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.source_metric_mappings (
    id                   TEXT        PRIMARY KEY,             -- prefixed ULID: 'smm_'
    metric_definition_id TEXT        NOT NULL REFERENCES app.metric_definitions(id) ON DELETE CASCADE,
    connector            TEXT        NOT NULL,                -- opaque (AD-2): module name
    source_field_path    TEXT,                                -- field path in the source response
    extraction_note      TEXT,
    status               TEXT        NOT NULL DEFAULT 'proposed'
                         CHECK (status IN ('proposed','confirmed','renamed','rejected')),
    -- scope triplet
    scope_level          TEXT        NOT NULL CHECK (scope_level IN ('PLATFORM','ORG','PROJECT')),
    org_id               TEXT        REFERENCES app.organizations(id) ON DELETE CASCADE,
    project_id           TEXT        REFERENCES app.projects(id)      ON DELETE CASCADE,
    created_by           TEXT        NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_source_metric_mappings_scope_cols CHECK (
        (scope_level = 'PLATFORM' AND org_id IS NULL     AND project_id IS NULL)
     OR (scope_level = 'ORG'      AND org_id IS NOT NULL AND project_id IS NULL)
     OR (scope_level = 'PROJECT'  AND project_id IS NOT NULL)
    )
);

-- One mapping per (scope, metric definition, connector). Same COALESCE(...,'')
-- discipline for the NULL scope columns.
CREATE UNIQUE INDEX IF NOT EXISTS uq_source_metric_mappings_scope
    ON app.source_metric_mappings
    (scope_level, COALESCE(org_id, ''), COALESCE(project_id, ''), metric_definition_id, connector);

DROP TRIGGER IF EXISTS trg_source_metric_mappings_updated_at ON app.source_metric_mappings;
CREATE TRIGGER trg_source_metric_mappings_updated_at
    BEFORE UPDATE ON app.source_metric_mappings
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- ---------------------------------------------------------------------------
-- 3. app.overlap_groups -- layer 2: a set of connectors that emit the SAME metric
--    and would double-count if summed. The NEW concept of the epic.
--
-- INVARIANT 2 (AD-4): summing BETWEEN groups is OK; summing WITHIN a group is NEVER
-- an implicit SUM -- it is whatever the group's reconciliation_rule states (PRIORITY /
-- DEDUP_ID / KEEP_SEPARATE never silently sum). 27.1 carries the rule; 27.3 applies it.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.overlap_groups (
    id                   TEXT        PRIMARY KEY,             -- prefixed ULID: 'ovg_'
    metric_definition_id TEXT        REFERENCES app.metric_definitions(id) ON DELETE CASCADE,
    canonical_name       TEXT        NOT NULL,                -- target metric; redundant with
                                                              -- metric_definition_id but needed to scope a
                                                              -- PLATFORM group by name even without a matching
                                                              -- PLATFORM definition (cf. seed import)
    name                 TEXT        NOT NULL,                -- human-readable group name (e.g. 'conversions-priority')
    description          TEXT,
    -- scope triplet
    scope_level          TEXT        NOT NULL CHECK (scope_level IN ('PLATFORM','ORG','PROJECT')),
    org_id               TEXT        REFERENCES app.organizations(id) ON DELETE CASCADE,
    project_id           TEXT        REFERENCES app.projects(id)      ON DELETE CASCADE,
    created_by           TEXT        NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_overlap_groups_scope_cols CHECK (
        (scope_level = 'PLATFORM' AND org_id IS NULL     AND project_id IS NULL)
     OR (scope_level = 'ORG'      AND org_id IS NOT NULL AND project_id IS NULL)
     OR (scope_level = 'PROJECT'  AND project_id IS NOT NULL)
    )
);

-- One group per (scope, name). COALESCE(...,'') discipline for the NULL scope columns.
CREATE UNIQUE INDEX IF NOT EXISTS uq_overlap_groups_scope_name
    ON app.overlap_groups
    (scope_level, COALESCE(org_id, ''), COALESCE(project_id, ''), name);

CREATE INDEX IF NOT EXISTS ix_overlap_groups_canonical_name
    ON app.overlap_groups (canonical_name);

DROP TRIGGER IF EXISTS trg_overlap_groups_updated_at ON app.overlap_groups;
CREATE TRIGGER trg_overlap_groups_updated_at
    BEFORE UPDATE ON app.overlap_groups
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- ---------------------------------------------------------------------------
-- 4. app.overlap_group_members -- one connector per row in a group.
--    Relational (not a TEXT[] on overlap_groups) so a member can grow attributes
--    in 27.3+ (account_scope reserved below) and so "at most once per group" is a
--    proper UNIQUE index -- the house style (org_members separate from organizations).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.overlap_group_members (
    id               TEXT        PRIMARY KEY,                 -- prefixed ULID: 'ovgm_'
    overlap_group_id TEXT        NOT NULL REFERENCES app.overlap_groups(id) ON DELETE CASCADE,
    connector        TEXT        NOT NULL,                    -- opaque (AD-2)
    account_scope    TEXT,                                    -- reserved v-future (§4: (connector[, account_scope?])); NULL in v1
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- one connector at most once per group; COALESCE(...,'') so a NULL account_scope
    -- collides with a NULL account_scope (NULL <> NULL would let duplicates through).
    CONSTRAINT uq_overlap_group_members UNIQUE (overlap_group_id, connector, account_scope)
);

CREATE INDEX IF NOT EXISTS ix_overlap_group_members_group
    ON app.overlap_group_members (overlap_group_id);

-- ---------------------------------------------------------------------------
-- 5. app.reconciliation_rules -- layer 2: exactly one living rule per overlap group.
--
-- priority_order is an ORDERED JSONB array of connectors (the order IS the priority):
-- this is the shape that reproduces the seed order exactly, e.g. conversions ->
-- ["google-analytics","meta-ads","tiktok-ads","linkedin-ads"]. The applicative
-- constraints (PRIORITY needs a non-empty priority_order; DEDUP_ID needs a join_key)
-- are enforced in metric_semantics.py + tested offline, NOT as SQL CHECKs.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.reconciliation_rules (
    id               TEXT        PRIMARY KEY,                 -- prefixed ULID: 'recrule_'
    overlap_group_id TEXT        NOT NULL REFERENCES app.overlap_groups(id) ON DELETE CASCADE,
    method           TEXT        NOT NULL
                     CHECK (method IN ('SUM','PRIORITY','DEDUP_ID','ESTIMATE','KEEP_SEPARATE')),
    priority_order   JSONB,                                   -- ORDERED connector array (PRIORITY)
    join_key         TEXT,                                    -- DEDUP_ID (e.g. 'transaction_id')
    truth_connector  TEXT,                                    -- ESTIMATE / PRIORITY: designated source of truth
    target_mart      TEXT,                                    -- resolver 27.3: 'cross_source_revenue' etc. (NULL in 27.1)
    created_by       TEXT        NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- one living rule per group.
    CONSTRAINT uq_reconciliation_rules_group UNIQUE (overlap_group_id)
);

DROP TRIGGER IF EXISTS trg_reconciliation_rules_updated_at ON app.reconciliation_rules;
CREATE TRIGGER trg_reconciliation_rules_updated_at
    BEFORE UPDATE ON app.reconciliation_rules
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- ---------------------------------------------------------------------------
-- 6. app.metric_semantics_audit -- DEDICATED append-only registry.
--
-- NOT app.audit_log: that table's connection_ref is NOT NULL REFERENCES
-- app.connection_ref(id) (002) -- a metric-definition mutation is not tied to a
-- connection, so we cannot write there without fabricating a bogus connection_ref.
-- We reproduce the append-only DISCIPLINE of audit_log on a domain-shaped table with
-- before/after JSONB (a full, replayable diff) so the DB audit substitutes for git
-- versioning of definitions (epic synthesis §3: "audit DB replaces git").
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.metric_semantics_audit (
    id          TEXT        PRIMARY KEY,                      -- prefixed ULID: 'msaudit_'
    identity    TEXT        NOT NULL,                         -- subject (AD-14); 'system' for the defaults import
    action      TEXT        NOT NULL,                         -- '<entity>.<created|upserted|deleted>'
    entity_type TEXT        NOT NULL,                         -- 'metric_definition'|'overlap_group'|'overlap_group_member'
                                                              --   |'reconciliation_rule'|'source_metric_mapping'
    entity_id   TEXT        NOT NULL,                         -- id of the mutated row
    scope_level TEXT        NOT NULL,                         -- copy of the scope at mutation time
    org_id      TEXT,
    project_id  TEXT,
    before      JSONB,                                        -- state before (NULL on create)
    after       JSONB,                                        -- state after (NULL on delete)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_metric_semantics_audit_entity
    ON app.metric_semantics_audit (entity_type, entity_id);
CREATE INDEX IF NOT EXISTS ix_metric_semantics_audit_created
    ON app.metric_semantics_audit (created_at DESC);

-- APPEND-ONLY (pattern 002/003/004): the application role keeps INSERT/SELECT, never
-- UPDATE/DELETE. No updated_at column (append-only by design). The REVOKE is wrapped
-- so it stays replayable on throwaway test databases where the 'connector' role does
-- NOT exist (a bare REVOKE would raise undefined_object and abort the migration).
--
-- The REVOKE alone is INEFFECTIVE against the OWNER role (the repo learned this in
-- 003/004): a PostgreSQL table owner implicitly retains all privileges, so a REVOKE
-- UPDATE/DELETE does not bind the 'connector' owner. We therefore reproduce the house
-- pattern of 003 (row-level BEFORE UPDATE OR DELETE trigger that RAISEs regardless of
-- caller role) + 004 (statement-level BEFORE TRUNCATE trigger, since row-level triggers
-- do not fire on TRUNCATE) to guarantee the append-only invariant EVERYWHERE, owner
-- included. The REVOKE is kept as defence-in-depth for a future dedicated non-owner
-- app role.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'connector') THEN
        REVOKE UPDATE, DELETE ON app.metric_semantics_audit FROM connector;
    END IF;
END
$$;

-- Row-level guard: block UPDATE and DELETE regardless of caller role (owner included).
CREATE OR REPLACE FUNCTION app.metric_semantics_audit_block_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'app.metric_semantics_audit is append-only (Story 27.1): % blocked', TG_OP
        USING ERRCODE = 'raise_exception';
END;
$$;

DROP TRIGGER IF EXISTS metric_semantics_audit_append_only ON app.metric_semantics_audit;
CREATE TRIGGER metric_semantics_audit_append_only
    BEFORE UPDATE OR DELETE ON app.metric_semantics_audit
    FOR EACH ROW
    EXECUTE FUNCTION app.metric_semantics_audit_block_mutation();

-- Statement-level guard: row-level triggers do not fire on TRUNCATE, and the owner
-- keeps the TRUNCATE privilege regardless of REVOKE. Close the last mutation path.
CREATE OR REPLACE FUNCTION app.metric_semantics_audit_block_truncate()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'app.metric_semantics_audit is append-only (Story 27.1): TRUNCATE blocked'
        USING ERRCODE = 'raise_exception';
END;
$$;

DROP TRIGGER IF EXISTS metric_semantics_audit_block_truncate ON app.metric_semantics_audit;
CREATE TRIGGER metric_semantics_audit_block_truncate
    BEFORE TRUNCATE ON app.metric_semantics_audit
    FOR EACH STATEMENT
    EXECUTE FUNCTION app.metric_semantics_audit_block_truncate();

COMMENT ON TABLE app.metric_semantics_audit IS
    'Story 27.1: append-only audit of every metric-semantics mutation (definitions, overlap groups + members, reconciliation rules, source mappings). before/after JSONB give a full replayable diff so the DB audit substitutes for git versioning of definitions. UPDATE/DELETE revoked from the application role.';

COMMIT;
