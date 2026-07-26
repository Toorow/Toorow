-- Story 40.4: immutable add-scope (tracked-entity / source-binding) cost/cardinality
-- previews and governed confirmation with a backfill decision. Twin of migration 058
-- (geographic add-scope, app.geographic_change_previews). Additive, idempotent,
-- project-scoped. NO fact, registry or publication-pointer mutation on preview.
-- Reuse-don't-refork (E40-AD3): the backfill EXECUTION is the Epic 12 candidate-publication
-- contract (bounded_recovery/refetch), not a new engine here; the preview shape is the
-- Epic 37 geographic_change machinery, keyed on the added entity/binding instead of a
-- geographic posture.
--
-- Migration number: 086 (40.1 tracked_entity_registry), 087
-- (connector_activations), 088 (import_templates), 089 (entity_source_bindings,
-- 40.2), 090 (inbound_brand_matching, 40.3), 091
-- (datastream_inbound_credentials) are all TAKEN; 092 is the next free number.
-- The former duplicate hourly migration was repaired as 110.
--
-- SOFT dependencies (documented, not enforced here beyond app.projects):
--   * entity_id references the 40.1 tracked-entity master (app.tracked_entities, 086). It
--     is kept a plain TEXT NOT NULL (a soft FK) so 40.4 does NOT hard-block on 086 being
--     applied to a throwaway test DB -- exactly as 058 kept the posture soft w.r.t. 057.
--     Promote to a REFERENCES app.tracked_entities(id) ON DELETE RESTRICT once 086 is a
--     guaranteed predecessor (Completion Notes).
--   * project_id references app.projects(id) ON DELETE RESTRICT (the hard, always-present FK,
--     copied verbatim from 058).
--
-- PENDING: NOT applied to Supabase (Jean's human gate, like 058/086/089). The whole file is
-- replayable (everything IF NOT EXISTS) so the orchestrator can apply it idempotently to a
-- throwaway DB for the pg-gated tests without a prod deploy gate.

BEGIN;
CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS app.entity_scope_change_previews (
    id                       TEXT        NOT NULL,
    project_id               TEXT        NOT NULL
                             REFERENCES app.projects(id) ON DELETE RESTRICT,
    entity_id                TEXT        NOT NULL,   -- the added tracked_entity (086); soft FK if 086 not yet applied
    proposed_add             JSONB       NOT NULL
                             CHECK (jsonb_typeof(proposed_add) = 'object'),  -- {kind: entity|binding, source?, query_spec?}
    impact                   JSONB       NOT NULL
                             CHECK (jsonb_typeof(impact) = 'object'),
    dependency_fingerprint   TEXT        NOT NULL
                             CHECK (dependency_fingerprint ~ '^[0-9a-f]{64}$'),
    status                   TEXT        NOT NULL DEFAULT 'pending'
                             CHECK (status IN ('pending', 'confirmed', 'superseded')),
    confirmation             JSONB
                             CHECK (confirmation IS NULL OR jsonb_typeof(confirmation) = 'object'),
    idempotency_key_hash     TEXT        NOT NULL
                             CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    requested_by             TEXT        NOT NULL,
    confirmed_by             TEXT,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmed_at             TIMESTAMPTZ,
    CONSTRAINT pk_entity_scope_change_previews PRIMARY KEY (id),
    CONSTRAINT uq_entity_scope_change_preview_idempotency
        UNIQUE (project_id, idempotency_key_hash),
    CONSTRAINT chk_entity_scope_change_confirmation
        CHECK (
            (status = 'pending'   AND confirmation IS NULL     AND confirmed_at IS NULL)
            OR (status = 'confirmed' AND confirmation IS NOT NULL AND confirmed_at IS NOT NULL)
            OR status = 'superseded'
        )
);

CREATE INDEX IF NOT EXISTS idx_entity_scope_change_previews_project
    ON app.entity_scope_change_previews (project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_entity_scope_change_previews_entity
    ON app.entity_scope_change_previews (entity_id);

COMMENT ON TABLE app.entity_scope_change_previews IS
    'Story 40.4: immutable, idempotent, no-write add-scope preview for adding a tracked_entity / entity_source_binding (E40-FR07). Twin of app.geographic_change_previews (058). Records the extra per-source query cost/cardinality and the backfill decision before activation; backfill EXECUTION reuses the Epic 12 candidate-publication contract (no candidate bypasses the DQ gate). A confirmed defer leaves existing figures unchanged and past periods honestly absent (E40-NFR06, AD-9).';

COMMIT;
