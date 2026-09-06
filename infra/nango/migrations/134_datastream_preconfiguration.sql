-- Story 47.2: immutable, evidence-backed pre-Datastream setup proposals.
BEGIN;

-- Migration 030 already created uq_datastreams_id_project as a bare unique
-- index, so ADD CONSTRAINT ... UNIQUE (...) raises 42P07 on the name instead of
-- adopting it. Promote that exact index; composite foreign keys below need a
-- real constraint.
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'app.datastreams'::regclass
          AND conname = 'uq_datastreams_id_project'
    ) THEN
        ALTER TABLE app.datastreams
            ADD CONSTRAINT uq_datastreams_id_project
            UNIQUE USING INDEX uq_datastreams_id_project;
    END IF;
END $$;

CREATE OR REPLACE FUNCTION app.safe_preconfiguration_evidence(value JSONB)
RETURNS BOOLEAN LANGUAGE sql IMMUTABLE AS $$
    SELECT jsonb_typeof(value) = 'object'
       AND octet_length(value::text) <= 8192
       AND lower(value::text) !~ '"(credential|secret|access_token|refresh_token|provider_account_id|raw_sample|raw_payload|authorization)"[[:space:]]*:';
$$;

CREATE TABLE IF NOT EXISTS app.datastream_setup_drafts (
    id TEXT NOT NULL PRIMARY KEY CHECK (id ~ '^dsd_[0-9A-HJKMNP-TV-Z]{26}$'),
    project_id TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    created_by TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft' CHECK (state IN ('draft','exited','materialized','archived')),
    current_revision_id TEXT,
    current_proposal_id TEXT,
    materialized_datastream_id TEXT,
    idempotency_key_hash TEXT NOT NULL CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (id, project_id),
    UNIQUE (project_id, idempotency_key_hash),
    FOREIGN KEY (materialized_datastream_id, project_id)
        REFERENCES app.datastreams(id, project_id) ON DELETE RESTRICT,
    CHECK ((state = 'materialized') = (materialized_datastream_id IS NOT NULL))
);
CREATE UNIQUE INDEX uq_datastream_setup_draft_resumable
    ON app.datastream_setup_drafts(project_id) WHERE state IN ('draft','exited');

CREATE TABLE IF NOT EXISTS app.datastream_setup_draft_revisions (
    id TEXT NOT NULL PRIMARY KEY CHECK (id ~ '^dsdr_[0-9A-HJKMNP-TV-Z]{26}$'),
    draft_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    revision_number INTEGER NOT NULL CHECK (revision_number >= 1),
    normalized_operator_input JSONB NOT NULL CHECK (app.safe_preconfiguration_evidence(normalized_operator_input)),
    first_incomplete_section TEXT NOT NULL,
    invalidation_causes JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(invalidation_causes) = 'array'),
    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    idempotency_key_hash TEXT NOT NULL CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    change_reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (draft_id, revision_number),
    UNIQUE (draft_id, content_hash),
    UNIQUE (draft_id, idempotency_key_hash),
    UNIQUE (id, draft_id, project_id),
    FOREIGN KEY (draft_id, project_id)
        REFERENCES app.datastream_setup_drafts(id, project_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS app.datastream_preconfiguration_proposals (
    id TEXT NOT NULL PRIMARY KEY CHECK (id ~ '^dspp_[0-9A-HJKMNP-TV-Z]{26}$'),
    draft_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    draft_revision_id TEXT NOT NULL,
    proposal_number INTEGER NOT NULL CHECK (proposal_number >= 1),
    schema_version TEXT NOT NULL,
    dependency_snapshot JSONB NOT NULL CHECK (jsonb_typeof(dependency_snapshot) = 'object'),
    dependency_fingerprint TEXT NOT NULL CHECK (dependency_fingerprint ~ '^[0-9a-f]{64}$'),
    section_fingerprints JSONB NOT NULL CHECK (jsonb_typeof(section_fingerprints) = 'object'),
    proposal_payload JSONB NOT NULL CHECK (jsonb_typeof(proposal_payload) = 'object'),
    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    idempotency_key_hash TEXT NOT NULL CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (draft_id, proposal_number),
    UNIQUE (draft_id, dependency_fingerprint, content_hash),
    UNIQUE (draft_id, idempotency_key_hash),
    UNIQUE (id, draft_id, project_id),
    FOREIGN KEY (draft_revision_id, draft_id, project_id)
        REFERENCES app.datastream_setup_draft_revisions(id, draft_id, project_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS app.datastream_preconfiguration_evidence_refs (
    proposal_id TEXT NOT NULL,
    draft_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    proposal_item_key TEXT NOT NULL,
    evidence_kind TEXT NOT NULL CHECK (evidence_kind IN ('connector_contract','observed_metadata','project_setting','governance_preset','operator_input')),
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    observed_at TIMESTAMPTZ NOT NULL,
    safe_metadata JSONB NOT NULL CHECK (
        octet_length(safe_metadata::text) <= 8192
        AND app.safe_preconfiguration_evidence(safe_metadata)),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (proposal_id, proposal_item_key, evidence_kind, object_type, object_id, version_id),
    FOREIGN KEY (proposal_id, draft_id, project_id)
        REFERENCES app.datastream_preconfiguration_proposals(id, draft_id, project_id) ON DELETE RESTRICT
);

ALTER TABLE app.datastream_setup_drafts ADD CONSTRAINT fk_datastream_setup_draft_current_revision
    FOREIGN KEY (current_revision_id, id, project_id)
    REFERENCES app.datastream_setup_draft_revisions(id, draft_id, project_id)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.datastream_setup_drafts ADD CONSTRAINT fk_datastream_setup_draft_current_proposal
    FOREIGN KEY (current_proposal_id, id, project_id)
    REFERENCES app.datastream_preconfiguration_proposals(id, draft_id, project_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE OR REPLACE FUNCTION app.reject_datastream_setup_draft_revision_mutation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$ BEGIN
    RAISE EXCEPTION 'datastream setup draft revisions are immutable' USING ERRCODE='23000';
END $$;
CREATE TRIGGER trg_datastream_setup_draft_revisions_immutable BEFORE UPDATE OR DELETE
    ON app.datastream_setup_draft_revisions FOR EACH ROW
    EXECUTE FUNCTION app.reject_datastream_setup_draft_revision_mutation();

CREATE OR REPLACE FUNCTION app.reject_datastream_preconfiguration_proposal_mutation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$ BEGIN
    RAISE EXCEPTION 'datastream preconfiguration proposals are immutable' USING ERRCODE='23000';
END $$;
CREATE TRIGGER trg_datastream_preconfiguration_proposals_immutable BEFORE UPDATE OR DELETE
    ON app.datastream_preconfiguration_proposals FOR EACH ROW
    EXECUTE FUNCTION app.reject_datastream_preconfiguration_proposal_mutation();

CREATE OR REPLACE FUNCTION app.reject_datastream_preconfiguration_evidence_mutation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$ BEGIN
    RAISE EXCEPTION 'datastream preconfiguration evidence is immutable' USING ERRCODE='23000';
END $$;
CREATE TRIGGER trg_datastream_preconfiguration_evidence_refs_immutable BEFORE UPDATE OR DELETE
    ON app.datastream_preconfiguration_evidence_refs FOR EACH ROW
    EXECUTE FUNCTION app.reject_datastream_preconfiguration_evidence_mutation();

CREATE OR REPLACE FUNCTION app.protect_datastream_setup_draft_identity()
RETURNS TRIGGER LANGUAGE plpgsql AS $$ BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.project_id IS DISTINCT FROM OLD.project_id
       OR NEW.created_by IS DISTINCT FROM OLD.created_by
       OR NEW.idempotency_key_hash IS DISTINCT FROM OLD.idempotency_key_hash
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'datastream setup draft identity is immutable' USING ERRCODE='23000';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_datastream_setup_draft_identity_immutable BEFORE UPDATE
    ON app.datastream_setup_drafts FOR EACH ROW
    EXECUTE FUNCTION app.protect_datastream_setup_draft_identity();

COMMIT;
