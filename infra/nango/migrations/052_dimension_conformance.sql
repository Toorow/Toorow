-- infra/nango/migrations/052_dimension_conformance.sql
--
-- Story 27.4: conformed dimensions -- VALUE-LEVEL unification of common dimensions
-- across sources (the "conformed dimensions" layer, point 5 of the epic model).
--
-- The manifests already carry the SCHEMA-level canonical_dimension_mapping
-- (source_dim -> canonical_dim, e.g. campaign_name -> campaign_name). What was
-- MISSING -- and what this migration provides -- is the VALUE-level mapping:
--   (canonical_dimension, connector, source_value) -> canonical_value
-- so the SAME campaign / placement named differently by Meta, Google, TikTok resolves
-- to ONE canonical value. The source_value is exactly a fact_daily_kpi.breakdown_value
-- (e.g. the campaign identity lives in breakdown_value WHEN breakdown_dimension =
-- 'campaign_id'); canonical_dimension is a CLIENT-CHOSEN conformed-dimension label
-- (e.g. 'campaign','placement','targeting') -- NOT necessarily equal to a
-- breakdown_dimension of the fact nor to a canonical_dimension_mapping key. The bridge
-- canonical_dimension <-> breakdown_dimension is documented here but NOT wired (the
-- warehouse read is deferred, see Story 27.4 Hors-perimetre).
--
-- SEMI-AUTO + HUMAN-VALIDATED: mappings are proposed semi-automatically (naming-pattern
-- normalization + difflib similarity, in server/core/dimension_conformance.py) and stay
-- status='proposed' until a human confirms. conform_value() resolves ONLY 'confirmed'
-- rows (AD-9 honesty: never a silent value fusion of an un-validated suggestion).
--
-- SCOPING: the (scope_level, org_id, project_id) triplet is repeated identically to
-- migration 049 (PLATFORM default / ORG override / PROJECT override). Decision (Jean):
-- the ORG is the anchor ("sales/campaigns mean the same thing across all projects",
-- epic 27 s54) -- but the full triplet + cascade are kept for a PROJECT exception
-- override without a new migration. Resolution (server/core/dimension_conformance.py)
-- is a cascade PROJECT > ORG > PLATFORM: the most specific confirmed row wins, pair by
-- pair.
--
-- STRICTLY ADDITIVE & PASSIVE (27.4): this migration CREATES the value-mapping table +
-- indexes only; NOTHING is wired onto the runtime. No warehouse row is read or written.
-- The values to reconcile are supplied by the caller (not read from the warehouse in
-- v1). Invariant 1 (native totals unchanged) is trivially satisfied -- no I/O on
-- fact_daily_kpi / cross_source_* / dbt.
--
-- AUDIT: NO new audit table. The append-only registry app.metric_semantics_audit
-- (migration 049, ALREADY APPLIED to Supabase) has a FREE entity_type TEXT column (no
-- enum CHECK -- verified in 049 lines 269-282). 27.4 writes there with
-- entity_type='dimension_value_mapping' via the shared _write_semantics_audit helper.
-- Fragmenting the audit per table would lose the unified "who changed what in this org's
-- semantics" view (epic synthesis s3: the DB audit substitutes for git versioning).
-- This migration therefore has a SOFT dependency on 049 (its audit table must exist).
--
-- ID prefixes (prefixed-ULID pattern): 'dvm_' (dimension_value_mappings). Audit rows
-- keep 049's 'msaudit_' prefix (shared table).
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/052_dimension_conformance.sql
--
-- Schema-Change-Checklist (CONTRIBUTING.md):
--   [x] Additive & idempotent (IF NOT EXISTS everywhere; the whole file is replayable)
--   [x] New columns are NULL-able or have defaults
--   [x] No destructive DROP/ALTER on populated columns
-- Verified: last real migration is 051 (render_snapshots). 050 (target_fields_governance)
-- and 051 are taken; this is 052 (first free).
--
-- PENDING -- NOT applied to Supabase (human gate: explicit go from Jean, like 046/048/
-- 049 before their go). The orchestrator may apply it to a throwaway test DB. 049 is
-- ALREADY applied in prod, so its audit table exists and 052 does NOT recreate it.
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- ---------------------------------------------------------------------------
-- app.dimension_value_mappings -- the VALUE-level mapping.
--
-- Scope triplet contract (repeated identically to 049, NOT imported):
--   PLATFORM => org_id IS NULL     AND project_id IS NULL
--   ORG      => org_id IS NOT NULL AND project_id IS NULL
--   PROJECT  => project_id IS NOT NULL   (org_id left OPTIONAL, see 049 note)
--
-- Decision (Completion Notes, same as 049): at PROJECT level org_id is OPTIONAL -- the
-- project already carries its own org_id in app.projects (035). ON DELETE CASCADE: the
-- disappearance of an org / project takes its specific value mappings with it; the
-- PLATFORM defaults survive.
--
-- STATUS semantics (SQL comment as the invariant contract):
--   'proposed'  -> a semi-auto suggestion; NEVER resolved by conform_value (returns None).
--   'confirmed' -> a human-validated mapping; the ONLY status conform_value resolves.
--   'rejected'  -> a recorded false-friend (do not re-suggest indefinitely); NOT resolved.
-- This is AD-9 applied to dimensions: no value fusion without human validation, even for
-- an 'exact'/'normalized' suggestion (they are only a HIGHER confidence, not a blank
-- cheque -- server/core/dimension_conformance.py never auto-confirms).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.dimension_value_mappings (
    id                   TEXT        PRIMARY KEY,          -- prefixed ULID: 'dvm_'
    canonical_dimension  TEXT        NOT NULL,             -- client-chosen conformed-dimension label (e.g. 'campaign','placement')
    connector            TEXT        NOT NULL,             -- opaque (AD-2): module name; == fact_daily_kpi.connector
    source_value         TEXT        NOT NULL,             -- value as emitted by the source (== breakdown_value)
    canonical_value      TEXT        NOT NULL,             -- unified value (what the card / the LLM will see)
    status               TEXT        NOT NULL DEFAULT 'proposed'
                         CHECK (status IN ('proposed','confirmed','rejected')),
    -- provenance of the (semi-auto) suggestion:
    suggestion_method    TEXT
                         CHECK (suggestion_method IS NULL
                                OR suggestion_method IN ('exact','normalized','similarity','manual')),
    suggestion_score     DOUBLE PRECISION,                -- [0,1] for 'similarity'; NULL/1.0 for exact/normalized/manual
    normalized_form      TEXT,                            -- normalized form retained (suggestion traceability)
    -- scope triplet (identical to 049)
    scope_level          TEXT        NOT NULL CHECK (scope_level IN ('PLATFORM','ORG','PROJECT')),
    org_id               TEXT        REFERENCES app.organizations(id) ON DELETE CASCADE,
    project_id           TEXT        REFERENCES app.projects(id)      ON DELETE CASCADE,
    created_by           TEXT        NOT NULL,             -- identity subject (AD-14); 'system' for an auto suggestion
    reviewed_by          TEXT,                            -- who confirmed/rejected (NULL while 'proposed')
    reviewed_at          TIMESTAMPTZ,                     -- when (NULL while 'proposed')
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_dimension_value_mappings_scope_cols CHECK (
        (scope_level = 'PLATFORM' AND org_id IS NULL     AND project_id IS NULL)
     OR (scope_level = 'ORG'      AND org_id IS NOT NULL AND project_id IS NULL)
     OR (scope_level = 'PROJECT'  AND project_id IS NOT NULL)
    ),
    CONSTRAINT ck_dimension_value_mappings_score CHECK (
        suggestion_score IS NULL OR (suggestion_score >= 0 AND suggestion_score <= 1)
    )
);

-- UNICITY: one mapping per (scope, canonical_dimension, connector, source_value). A
-- source value of a connector for a given dimension maps only ONCE per scope. The
-- COALESCE(...,'') is MANDATORY (NULL <> NULL in a plain composite index would let two
-- PLATFORM/ORG rows with a NULL project_id both through, breaking the invariant). Same
-- discipline as the 4 tables of 049.
CREATE UNIQUE INDEX IF NOT EXISTS uq_dimension_value_mappings_scope_key
    ON app.dimension_value_mappings
    (scope_level, COALESCE(org_id, ''), COALESCE(project_id, ''),
     canonical_dimension, connector, source_value);

-- Resolution path (conform_value / resolve_dimension_conformance):
-- (canonical_dimension, connector, source_value, status) -> canonical_value.
CREATE INDEX IF NOT EXISTS ix_dimension_value_mappings_resolve
    ON app.dimension_value_mappings
    (canonical_dimension, connector, source_value, status);

-- "every source value attached to one canonical value" (grouping / review UI).
CREATE INDEX IF NOT EXISTS ix_dimension_value_mappings_canonical
    ON app.dimension_value_mappings
    (canonical_dimension, canonical_value);

-- updated_at trigger (reuse app.set_updated_at() from migration 023, like 049).
DROP TRIGGER IF EXISTS trg_dimension_value_mappings_updated_at ON app.dimension_value_mappings;
CREATE TRIGGER trg_dimension_value_mappings_updated_at
    BEFORE UPDATE ON app.dimension_value_mappings
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

COMMENT ON TABLE app.dimension_value_mappings IS
    'Story 27.4: value-level conformed-dimension mapping ((canonical_dimension, connector, source_value) -> canonical_value). Semi-auto proposed (naming normalization + difflib similarity), human-validated. status=confirmed is the ONLY status resolved by conform_value (AD-9: no silent value fusion). canonical_dimension is a client label bridging to fact_daily_kpi.breakdown_dimension (bridge documented, not wired: warehouse read deferred). Mutations audited in app.metric_semantics_audit (049) with entity_type=dimension_value_mapping.';

COMMIT;
