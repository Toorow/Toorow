-- Story 47.3: immutable revision-bound Datastream setup observations and assets.
BEGIN;

CREATE TABLE app.datastream_setup_assets (
    id TEXT PRIMARY KEY CHECK (id ~ '^dsa_[0-9A-HJKMNP-TV-Z]{26}$'),
    project_id TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    draft_id TEXT NOT NULL,
    storage_ref TEXT NOT NULL CHECK (octet_length(storage_ref) <= 1024),
    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    detected_format TEXT CHECK (detected_format IN ('csv','tsv','xlsx','sav','unknown')),
    byte_count BIGINT NOT NULL CHECK (byte_count >= 0),
    state TEXT NOT NULL DEFAULT 'available' CHECK (state IN ('available','expired','quarantined')),
    expires_at TIMESTAMPTZ NOT NULL,
    created_by TEXT NOT NULL,
    cleanup_owner TEXT NOT NULL DEFAULT 'datastream_setup_asset_retention',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (id, draft_id, project_id),
    UNIQUE (draft_id, content_hash),
    FOREIGN KEY (draft_id, project_id)
        REFERENCES app.datastream_setup_drafts(id, project_id) ON DELETE RESTRICT,
    CHECK (expires_at > created_at)
);

CREATE TABLE app.datastream_setup_observations (
    id TEXT PRIMARY KEY CHECK (id ~ '^dso_[0-9A-HJKMNP-TV-Z]{26}$'),
    project_id TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    draft_id TEXT NOT NULL,
    draft_revision_id TEXT NOT NULL,
    draft_revision INTEGER NOT NULL CHECK (draft_revision >= 1),
    mode TEXT NOT NULL CHECK (mode IN ('connector_pull','external_bq','managed_feed')),
    discovery_kind TEXT NOT NULL CHECK (discovery_kind IN (
        'connector_contract','connector_fields','warehouse_schema',
        'file_schema','sheet_schema','channel_contract')),
    adapter_ref TEXT NOT NULL,
    connector_contract_version_ref TEXT REFERENCES app.connector_contract_versions(id) ON DELETE RESTRICT,
    request_fingerprint TEXT NOT NULL CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    evidence_fingerprint TEXT NOT NULL CHECK (evidence_fingerprint ~ '^[0-9a-f]{64}$'),
    schema_hash TEXT CHECK (schema_hash IS NULL OR schema_hash ~ '^[0-9a-f]{64}$'),
    safe_metadata JSONB NOT NULL CHECK (app.safe_preconfiguration_evidence(safe_metadata)),
    coverage JSONB NOT NULL CHECK (jsonb_typeof(coverage) = 'object' AND octet_length(coverage::text) <= 4096),
    exceptions JSONB NOT NULL CHECK (jsonb_typeof(exceptions) = 'array' AND octet_length(exceptions::text) <= 4096),
    staged_asset_id TEXT,
    idempotency_key_hash TEXT NOT NULL CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    observed_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (id, draft_id, project_id),
    UNIQUE (draft_id, idempotency_key_hash),
    UNIQUE (draft_id, draft_revision_id, request_fingerprint, evidence_fingerprint),
    FOREIGN KEY (draft_revision_id, draft_id, project_id)
        REFERENCES app.datastream_setup_draft_revisions(id, draft_id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (staged_asset_id, draft_id, project_id)
        REFERENCES app.datastream_setup_assets(id, draft_id, project_id) ON DELETE RESTRICT,
    CHECK (expires_at IS NULL OR expires_at > observed_at)
);

CREATE INDEX idx_datastream_setup_observations_current
    ON app.datastream_setup_observations(draft_id, draft_revision DESC, observed_at DESC);
CREATE INDEX idx_datastream_setup_assets_expiry
    ON app.datastream_setup_assets(state, expires_at);

CREATE OR REPLACE FUNCTION app.reject_datastream_setup_evidence_mutation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$ BEGIN
    RAISE EXCEPTION 'Datastream setup evidence is append-only' USING ERRCODE='23000';
END $$;
CREATE TRIGGER trg_datastream_setup_observations_immutable BEFORE UPDATE OR DELETE
    ON app.datastream_setup_observations FOR EACH ROW
    EXECUTE FUNCTION app.reject_datastream_setup_evidence_mutation();
CREATE OR REPLACE FUNCTION app.guard_datastream_setup_asset_lifecycle()
RETURNS TRIGGER LANGUAGE plpgsql AS $$ BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Datastream setup asset evidence cannot be deleted' USING ERRCODE='23000';
    END IF;
    IF OLD.project_id <> NEW.project_id OR OLD.draft_id <> NEW.draft_id
       OR OLD.storage_ref <> NEW.storage_ref OR OLD.content_hash <> NEW.content_hash
       OR OLD.byte_count <> NEW.byte_count OR OLD.expires_at <> NEW.expires_at
       OR OLD.created_by <> NEW.created_by OR OLD.created_at <> NEW.created_at
       OR NOT (OLD.state = 'available' AND NEW.state IN ('expired','quarantined')) THEN
        RAISE EXCEPTION 'Only the setup asset retention state may advance' USING ERRCODE='23000';
    END IF;
    NEW.updated_at := NOW();
    RETURN NEW;
END $$;
CREATE TRIGGER trg_datastream_setup_assets_lifecycle BEFORE UPDATE OR DELETE
    ON app.datastream_setup_assets FOR EACH ROW
    EXECUTE FUNCTION app.guard_datastream_setup_asset_lifecycle();

COMMIT;
