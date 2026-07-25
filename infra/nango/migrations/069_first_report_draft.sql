-- Story 36.8: bounded first-report draft tracking.
--
-- Additive and idempotent (mirrors 065/068). A "draft" is expressible as an
-- immutable plan version (app.datastream_plan_versions, migration 030) plus an
-- immutable mapping version (app.datastream_mapping_versions, migration 032) that
-- are NOT yet the live pointers. Those version tables are reused verbatim -- this
-- migration adds ONLY the small tracking row that links the recommended selection
-- to the exact saved plan/mapping versions and the audit operation, so the draft
-- can be previewed and later executed/published (Story 36.9) without a second
-- Datastream engine or a second mapping store (E36-NFR05).
--
-- The saved plan/mapping versions carry the capability/catalog versions already
-- (capability_contract_version / capability_fingerprint on those rows); the draft
-- row keeps a redacted, model-safe copy of the recommended selection for display.
-- NO provider payload, token, or raw sample is ever stored here.

BEGIN;

CREATE TABLE IF NOT EXISTS app.first_report_drafts (
    id                  TEXT        PRIMARY KEY,
    datastream_id       TEXT        NOT NULL,
    project_id          TEXT        NOT NULL,
    plan_version_id     TEXT        NOT NULL,
    mapping_version_id  TEXT        NOT NULL,
    -- Redacted, model-safe recommendation snapshot (report/metrics/dimensions/
    -- grain/timezone/currency/interval + capability versions). Identifiers only.
    recommendation      JSONB       NOT NULL CHECK (jsonb_typeof(recommendation) = 'object'),
    state               TEXT        NOT NULL DEFAULT 'saved'
                        CHECK (state IN ('saved', 'previewed', 'superseded')),
    operation_id        TEXT        REFERENCES app.operations(id) ON DELETE RESTRICT,
    created_by          TEXT        NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- The draft binds to an EXACT saved plan version for this datastream/project.
    CONSTRAINT fk_first_report_draft_plan_scope
        FOREIGN KEY (plan_version_id, datastream_id, project_id)
        REFERENCES app.datastream_plan_versions (id, datastream_id, project_id)
        ON DELETE RESTRICT,
    -- ... and an EXACT saved mapping version for the same datastream/project.
    CONSTRAINT fk_first_report_draft_mapping_scope
        FOREIGN KEY (mapping_version_id, datastream_id, project_id)
        REFERENCES app.datastream_mapping_versions (id, datastream_id, project_id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_first_report_draft_operation UNIQUE (operation_id)
);

CREATE INDEX IF NOT EXISTS first_report_drafts_datastream
    ON app.first_report_drafts (datastream_id, project_id, created_at DESC);

-- The saved version references are immutable after creation: only `state` and
-- `updated_at` may change. Mirrors 068's protect_source_delegation_binding.
CREATE OR REPLACE FUNCTION app.protect_first_report_draft_binding()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.datastream_id IS DISTINCT FROM OLD.datastream_id
       OR NEW.project_id IS DISTINCT FROM OLD.project_id
       OR NEW.plan_version_id IS DISTINCT FROM OLD.plan_version_id
       OR NEW.mapping_version_id IS DISTINCT FROM OLD.mapping_version_id
       OR NEW.recommendation IS DISTINCT FROM OLD.recommendation
       OR NEW.operation_id IS DISTINCT FROM OLD.operation_id
       OR NEW.created_by IS DISTINCT FROM OLD.created_by THEN
        RAISE EXCEPTION 'first report draft binding is immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_first_report_draft_binding_immutable ON app.first_report_drafts;
CREATE TRIGGER trg_first_report_draft_binding_immutable
    BEFORE UPDATE ON app.first_report_drafts
    FOR EACH ROW EXECUTE FUNCTION app.protect_first_report_draft_binding();

COMMIT;
