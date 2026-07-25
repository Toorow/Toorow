-- infra/nango/migrations/089_entity_source_bindings.sql
--
-- Story 40.2: ORG-scoped per-source binding -- wire the OUTBOUND collection.
--
-- The OUTBOUND half of Epic 40 (competitor / brand registry, E40-AD2). It HANGS OFF the
-- 40.1 ORG entity master (app.tracked_entities, migration 086) and adds exactly ONE thing:
-- the per-source BINDING that says HOW to ask a source about an entity. One table and
-- nothing more:
--
--   1. app.entity_source_bindings -- the ORG-scoped, SOURCE-AGNOSTIC query driver
--      (E40-FR03): one row per (entity, source, [account_scope]) carrying an OPAQUE
--      source handle, an OPAQUE external_id, and a free-form query_spec JSONB blob.
--      Declaring a brand ONCE (40.1) + a binding per source wires the outbound queries
--      for every source AUTOMATICALLY -- the queries are DERIVED from the registry, never
--      re-typed per datastream.
--
-- SOURCE-AGNOSTIC (E40-NFR03 / E40-AD2): source, external_id and query_spec are OPAQUE org
-- DATA -- core never interprets them. NO connector name, NO provider vocabulary, NO
-- query-shape keyword lives in this table or the store. Each connector declares HOW it
-- consumes a brand id in its OWN descriptor (source-capabilities.schema.json); core owns
-- WHAT (the opaque binding). The connector-side pull-setup consumption of the binding is
-- each source's own wiring (40.4 previews its cost, 40.5 exposes it under the governance
-- MCP profile) -- NOT wired here.
--
-- SCOPING: the binding hangs off an ORG entity, so it is ORG-scoped ONLY. We keep a
-- scope_level column with a stricter CHECK (scope_level = 'ORG') -- rather than dropping it
-- -- for family parity with 086/049/052, so a future PLATFORM/PROJECT relaxation (Phase B)
-- becomes a CHECK relaxation, not a schema fork. org_id is NOT NULL and DENORMALISED from
-- the entity (single-table cross-org existence-hiding + per-source listing reads). The
-- scope-triplet CHECK is repeated in SQL (SQL cannot import); the Python
-- validate_scope(SCOPE_ORG, org_id, None) (server/core/metric_semantics.py) is the shared
-- mirror used by the store + fake tests, driven off the entity's own org.
--
-- CROSS-ORG INTEGRITY: a binding's org_id MUST equal its entity's org_id
-- (binding.org_id == tracked_entities.org_id). This is enforced by a Python store guard
-- (server/core/entity_source_bindings.upsert_source_binding sets org_id FROM the entity,
-- never a caller-supplied org that disagrees), NOT a SQL trigger: the store is the single
-- write path in v1 (documented in the story Completion Notes). The FKs below guarantee
-- referential integrity; the org-equality guard lives in the store.
--
-- STRICTLY ADDITIVE & PASSIVE (40.2): this migration CREATES one table + indexes + trigger
-- only; NOTHING is wired onto the runtime. No warehouse row and no existing join is read or
-- written (E40-NFR06 satisfied by inaction -- a datastream with no binding behaves exactly
-- as before). A connector reading resolve_source_binding is each source's own follow-up.
--
-- AUDIT: NO new audit table. The append-only registry app.metric_semantics_audit
-- (migration 049, ALREADY APPLIED to Supabase) has a FREE entity_type TEXT column (no enum
-- CHECK). 40.2 writes there with entity_type='entity_source_binding' via the shared
-- _write_semantics_audit helper. The append-only enforcement (REVOKE UPDATE/DELETE) already
-- lives on that table from 049 -- 089 adds nothing there. This migration therefore has a
-- SOFT dependency on 049 (its audit table must exist) and on 086 (the FK target
-- app.tracked_entities); 049 is already in prod and 086 is PENDING alongside this one.
--
-- ID prefixes (prefixed-ULID pattern): 'esb_' (entity_source_bindings). Audit rows keep
-- 049's 'msaudit_' prefix (shared table).
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/089_entity_source_bindings.sql
--
-- Schema-Change-Checklist (CONTRIBUTING.md):
--   [x] Additive & idempotent (IF NOT EXISTS everywhere; the whole file is replayable)
--   [x] New columns are NULL-able or have defaults
--   [x] No destructive DROP/ALTER on populated columns
-- Verified at dev time: 086 is the last of the tracked-entity family, but 087 is TAKEN
-- TWICE (087_connector_activations, 087_datastream_hourly_schedule) and 088 is TAKEN
-- (088_import_templates); 089 is the next genuinely-free number (noted in the story
-- Completion Notes).
--
-- PENDING -- NOT applied to Supabase (human gate: explicit go from Jean, like 086/052/046
-- before their go). The orchestrator may apply it to a throwaway test DB. 049 is ALREADY
-- applied in prod, so its audit table exists and 089 does NOT recreate it.
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- ---------------------------------------------------------------------------
-- app.entity_source_bindings -- the source-agnostic query driver (E40-FR03).
--
-- ORG-scoped ONLY: scope_level is always 'ORG' (the binding hangs off an ORG entity),
-- org_id is NOT NULL and DENORMALISED from the entity (single-table cross-org read).
-- source / external_id / query_spec are OPAQUE org DATA -- core never interprets them
-- (E40-NFR03 / AD-2). Either external_id (the common single-id case) or query_spec (the
-- free-form driver) may drive a source; core requires neither over the other.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.entity_source_bindings (
    id             TEXT        PRIMARY KEY,                 -- prefixed ULID: 'esb_'
    entity_id      TEXT        NOT NULL REFERENCES app.tracked_entities(id) ON DELETE CASCADE,
    org_id         TEXT        NOT NULL REFERENCES app.organizations(id)    ON DELETE CASCADE,
    scope_level    TEXT        NOT NULL DEFAULT 'ORG' CHECK (scope_level = 'ORG'),
    source         TEXT        NOT NULL,                    -- OPAQUE source handle (a module name); AD-2: core never interprets it
    account_scope  TEXT,                                    -- optional opaque sub-scope (e.g. a specific account/property); NULL = the source's default
    external_id    TEXT,                                    -- OPAQUE per-source identifier (e.g. a club id, an advertiser id); NULL when the query_spec is the driver
    query_spec     JSONB       NOT NULL DEFAULT '{}'::jsonb, -- free-form, source-agnostic query driver (e.g. a term, a keyword set, handles); core never reads its shape
    status         TEXT        NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active','disabled')), -- disabled = declared-but-not-collected (40.4 activation seam); no provider meaning
    created_by     TEXT        NOT NULL,                    -- identity subject (AD-14)
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- UNICITY: ONE binding per (entity, source, account_scope). An entity is bound to a source
-- at most once per account_scope (the COALESCE mirrors the 049/052 NULL-safe pattern: two
-- bindings with account_scope NULL for the same (entity, source) would otherwise both be
-- accepted because NULL != NULL in a plain composite unique index).
CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_source_bindings_entity_source
    ON app.entity_source_bindings (entity_id, source, COALESCE(account_scope, ''));

-- Resolution / listing by source within an org (the "derive the queries" surface, AC2):
-- every entity's binding for one source of one org.
CREATE INDEX IF NOT EXISTS ix_entity_source_bindings_org_source
    ON app.entity_source_bindings (org_id, source, status);

-- Per-entity listing (one entity across all its sources).
CREATE INDEX IF NOT EXISTS ix_entity_source_bindings_entity
    ON app.entity_source_bindings (entity_id);

-- GIN on query_spec for a future spec-shape lookup (40.4/40.5); harmless additive index in v1.
CREATE INDEX IF NOT EXISTS ix_entity_source_bindings_query_spec
    ON app.entity_source_bindings USING gin (query_spec jsonb_path_ops);

DROP TRIGGER IF EXISTS trg_entity_source_bindings_updated_at ON app.entity_source_bindings;
CREATE TRIGGER trg_entity_source_bindings_updated_at
    BEFORE UPDATE ON app.entity_source_bindings
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

COMMENT ON TABLE app.entity_source_bindings IS
    'Story 40.2: ORG-scoped per-source binding (entity, source, external_id / query_spec) -- the OUTBOUND half of Epic 40 (E40-FR03, E40-AD2). Hangs off the 40.1 ORG entity master (app.tracked_entities): declaring a brand once wires the queries for every source, DERIVED from the registry, not re-typed per datastream. SOURCE-AGNOSTIC (E40-NFR03 / AD-2): source, external_id and query_spec are OPAQUE org DATA -- core never interprets them; each connector declares HOW it consumes a brand id in its own descriptor. Mutations audited in app.metric_semantics_audit (049) with entity_type=entity_source_binding. No warehouse row / existing join read or written (E40-NFR06).';

COMMIT;
