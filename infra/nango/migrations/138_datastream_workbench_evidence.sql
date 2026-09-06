-- Story 47.5: append-only execution-stage and physical Output evidence.
BEGIN;

CREATE TABLE app.datastream_execution_stage_evidence (
    id TEXT PRIMARY KEY CHECK (id ~ '^dsse_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    datastream_id TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (stage IN ('collected','mapped','processed','published')),
    phase_state TEXT NOT NULL CHECK (
        phase_state IN ('unavailable','queued','running','succeeded','failed','cancelled','outcome_unknown')
    ),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    interval_start TIMESTAMPTZ,
    interval_end TIMESTAMPTZ,
    plan_version_id TEXT NOT NULL,
    mapping_version_id TEXT NOT NULL,
    projection_version_ref TEXT,
    artifact_ref TEXT,
    materialization_ref TEXT,
    schema_hash TEXT CHECK (schema_hash IS NULL OR schema_hash ~ '^[0-9a-f]{64}$'),
    profile_evidence JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (app.safe_preconfiguration_evidence(profile_evidence)),
    coverage_evidence JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (app.safe_preconfiguration_evidence(coverage_evidence)),
    row_count BIGINT CHECK (row_count IS NULL OR row_count >= 0),
    grain_evidence JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (app.safe_preconfiguration_evidence(grain_evidence)),
    safe_error JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (app.safe_preconfiguration_evidence(safe_error)),
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (app.safe_preconfiguration_evidence(evidence)),
    created_by TEXT NOT NULL,
    UNIQUE (execution_id, stage),
    CHECK (
        (interval_start IS NULL AND interval_end IS NULL)
        OR (interval_start IS NOT NULL AND interval_end IS NOT NULL AND interval_start < interval_end)
    ),
    FOREIGN KEY (execution_id, datastream_id, project_id)
        REFERENCES app.datastream_executions(id, datastream_id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (datastream_id, project_id)
        REFERENCES app.datastreams(id, project_id) ON DELETE RESTRICT
);

CREATE INDEX idx_datastream_stage_evidence_timeline
    ON app.datastream_execution_stage_evidence(project_id, datastream_id, execution_id, occurred_at);

CREATE TABLE app.datastream_execution_phase_evidence (
    id TEXT PRIMARY KEY CHECK (id ~ '^dspe_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    datastream_id TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN ('pull','import','load','mapping','processing','dq','publication')),
    phase_state TEXT NOT NULL CHECK (
        phase_state IN ('unavailable','queued','running','succeeded','failed','cancelled','outcome_unknown')
    ),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    interval_start TIMESTAMPTZ,
    interval_end TIMESTAMPTZ,
    plan_version_id TEXT NOT NULL,
    mapping_version_id TEXT NOT NULL,
    trace_ref TEXT,
    artifact_ref TEXT,
    row_count BIGINT CHECK (row_count IS NULL OR row_count >= 0),
    quota_evidence JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (app.safe_preconfiguration_evidence(quota_evidence)),
    safe_error JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (app.safe_preconfiguration_evidence(safe_error)),
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (app.safe_preconfiguration_evidence(evidence)),
    created_by TEXT NOT NULL,
    UNIQUE (execution_id, phase),
    CHECK (
        (interval_start IS NULL AND interval_end IS NULL)
        OR (interval_start IS NOT NULL AND interval_end IS NOT NULL AND interval_start < interval_end)
    ),
    FOREIGN KEY (execution_id, datastream_id, project_id)
        REFERENCES app.datastream_executions(id, datastream_id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (datastream_id, project_id)
        REFERENCES app.datastreams(id, project_id) ON DELETE RESTRICT
);

CREATE INDEX idx_datastream_phase_evidence_timeline
    ON app.datastream_execution_phase_evidence(project_id, datastream_id, execution_id, occurred_at);

CREATE TABLE app.datastream_outputs (
    id TEXT PRIMARY KEY CHECK (id ~ '^dso_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    datastream_id TEXT NOT NULL,
    output_kind TEXT NOT NULL CHECK (output_kind IN ('full_grain','safe_projection','delivery')),
    stable_name TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, datastream_id, output_kind, stable_name),
    FOREIGN KEY (datastream_id, project_id)
        REFERENCES app.datastreams(id, project_id) ON DELETE RESTRICT
);

CREATE TABLE app.datastream_output_versions (
    id TEXT PRIMARY KEY CHECK (id ~ '^dsov_[0-9A-HJKMNP-TV-Z]{26}$'),
    output_id TEXT NOT NULL REFERENCES app.datastream_outputs(id) ON DELETE RESTRICT,
    org_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    datastream_id TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    publication_log_id TEXT,
    plan_version_id TEXT NOT NULL,
    mapping_version_id TEXT NOT NULL,
    projection_version_ref TEXT,
    relation_ref TEXT,
    delivery_ref TEXT,
    schema_hash TEXT CHECK (schema_hash IS NULL OR schema_hash ~ '^[0-9a-f]{64}$'),
    grain_evidence JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (app.safe_preconfiguration_evidence(grain_evidence)),
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (app.safe_preconfiguration_evidence(evidence)),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (output_id, execution_id),
    FOREIGN KEY (execution_id, datastream_id, project_id)
        REFERENCES app.datastream_executions(id, datastream_id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (datastream_id, project_id)
        REFERENCES app.datastreams(id, project_id) ON DELETE RESTRICT
);

CREATE TABLE app.datastream_output_used_by (
    output_id TEXT NOT NULL REFERENCES app.datastream_outputs(id) ON DELETE RESTRICT,
    output_version_id TEXT NOT NULL REFERENCES app.datastream_output_versions(id) ON DELETE RESTRICT,
    project_id TEXT NOT NULL,
    consumer_kind TEXT NOT NULL CHECK (consumer_kind IN ('semantic_view','report','result','delivery')),
    consumer_ref TEXT NOT NULL,
    consumer_version_ref TEXT,
    owner_href TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (output_version_id, consumer_kind, consumer_ref)
);

CREATE TABLE app.datastream_change_preparations (
    id TEXT PRIMARY KEY CHECK (id ~ '^dscp_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    datastream_id TEXT NOT NULL,
    change_kind TEXT NOT NULL CHECK (change_kind IN ('mapping','processing')),
    expected_plan_version_id TEXT NOT NULL,
    expected_mapping_version_id TEXT NOT NULL,
    proposed_payload JSONB NOT NULL CHECK (app.safe_preconfiguration_evidence(proposed_payload)),
    review JSONB NOT NULL CHECK (app.safe_preconfiguration_evidence(review)),
    review_hash TEXT NOT NULL CHECK (review_hash ~ '^[0-9a-f]{64}$'),
    confirmation_secret_hash TEXT NOT NULL CHECK (confirmation_secret_hash ~ '^[0-9a-f]{64}$'),
    idempotency_key_hash TEXT NOT NULL CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    state TEXT NOT NULL DEFAULT 'prepared' CHECK (state IN ('prepared','confirmed','expired')),
    prepared_by TEXT NOT NULL,
    prepared_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    confirmed_at TIMESTAMPTZ,
    operation_id TEXT REFERENCES app.operations(id) ON DELETE RESTRICT,
    candidate_execution_id TEXT,
    UNIQUE (project_id, idempotency_key_hash),
    FOREIGN KEY (datastream_id, project_id)
        REFERENCES app.datastreams(id, project_id) ON DELETE RESTRICT
);

CREATE INDEX idx_datastream_change_preparations_scope
    ON app.datastream_change_preparations(project_id, datastream_id, prepared_at DESC);
CREATE TABLE app.datastream_rollback_preparations (
    id TEXT PRIMARY KEY CHECK (id ~ '^dsrp_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    datastream_id TEXT NOT NULL,
    expected_current_execution_id TEXT NOT NULL,
    target_execution_id TEXT NOT NULL,
    review JSONB NOT NULL CHECK (app.safe_preconfiguration_evidence(review)),
    review_hash TEXT NOT NULL CHECK (review_hash ~ '^[0-9a-f]{64}$'),
    confirmation_secret_hash TEXT NOT NULL CHECK (confirmation_secret_hash ~ '^[0-9a-f]{64}$'),
    state TEXT NOT NULL DEFAULT 'prepared' CHECK (state IN ('prepared','confirmed','expired')),
    prepared_by TEXT NOT NULL,
    prepared_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    confirmed_at TIMESTAMPTZ,
    publication_log_id TEXT,
    operation_id TEXT REFERENCES app.operations(id) ON DELETE RESTRICT,
    FOREIGN KEY (datastream_id, project_id)
        REFERENCES app.datastreams(id, project_id) ON DELETE RESTRICT
);
CREATE OR REPLACE FUNCTION app.reject_datastream_workbench_evidence_mutation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$ BEGIN
    RAISE EXCEPTION 'Datastream Workbench evidence is immutable' USING ERRCODE='23000';
END $$;

CREATE TRIGGER trg_datastream_stage_evidence_immutable BEFORE UPDATE OR DELETE
    ON app.datastream_execution_stage_evidence FOR EACH ROW
    EXECUTE FUNCTION app.reject_datastream_workbench_evidence_mutation();
CREATE TRIGGER trg_datastream_phase_evidence_immutable BEFORE UPDATE OR DELETE
    ON app.datastream_execution_phase_evidence FOR EACH ROW
    EXECUTE FUNCTION app.reject_datastream_workbench_evidence_mutation();
CREATE TRIGGER trg_datastream_outputs_immutable BEFORE UPDATE OR DELETE
    ON app.datastream_outputs FOR EACH ROW
    EXECUTE FUNCTION app.reject_datastream_workbench_evidence_mutation();
CREATE TRIGGER trg_datastream_output_versions_immutable BEFORE UPDATE OR DELETE
    ON app.datastream_output_versions FOR EACH ROW
    EXECUTE FUNCTION app.reject_datastream_workbench_evidence_mutation();
CREATE TRIGGER trg_datastream_output_used_by_immutable BEFORE UPDATE OR DELETE
    ON app.datastream_output_used_by FOR EACH ROW
    EXECUTE FUNCTION app.reject_datastream_workbench_evidence_mutation();

COMMIT;
