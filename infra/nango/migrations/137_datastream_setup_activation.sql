-- Story 47.4: immutable setup preview/review evidence and Draft materialization.
BEGIN;

ALTER TABLE app.datastreams ADD COLUMN IF NOT EXISTS lifecycle_state TEXT;
UPDATE app.datastreams
SET lifecycle_state = CASE
    WHEN current_published_execution_id IS NULL THEN 'draft'
    WHEN enabled THEN 'active'
    ELSE 'paused'
END
WHERE lifecycle_state IS NULL;
ALTER TABLE app.datastreams ALTER COLUMN lifecycle_state SET DEFAULT 'draft';
ALTER TABLE app.datastreams ALTER COLUMN lifecycle_state SET NOT NULL;
ALTER TABLE app.datastreams DROP CONSTRAINT IF EXISTS ck_datastream_lifecycle_state;
ALTER TABLE app.datastreams ADD CONSTRAINT ck_datastream_lifecycle_state
    CHECK (lifecycle_state IN ('draft', 'active', 'paused', 'archived'));

-- Only the active plan may be due; Draft and non-current versions stay inert.
UPDATE app.datastream_schedule_state s
SET next_run_at = NULL
FROM app.datastreams d
WHERE s.datastream_id = d.id AND s.project_id = d.project_id
  AND (d.lifecycle_state <> 'active' OR s.plan_version_id IS DISTINCT FROM d.current_plan_version_id);

CREATE TABLE app.datastream_setup_previews (
    id TEXT PRIMARY KEY CHECK (id ~ '^dspv_[0-9A-HJKMNP-TV-Z]{26}$'),
    project_id TEXT NOT NULL,
    draft_id TEXT NOT NULL,
    draft_revision_id TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('connector_pull','external_bq','managed_feed')),
    dependency_snapshot JSONB NOT NULL CHECK (app.safe_preconfiguration_evidence(dependency_snapshot)),
    dependency_hash TEXT NOT NULL CHECK (dependency_hash ~ '^[0-9a-f]{64}$'),
    mapping_hash TEXT NOT NULL CHECK (mapping_hash ~ '^[0-9a-f]{64}$'),
    processing_hash TEXT NOT NULL CHECK (processing_hash ~ '^[0-9a-f]{64}$'),
    safe_evidence JSONB NOT NULL CHECK (app.safe_preconfiguration_evidence(safe_evidence)),
    evidence_hash TEXT NOT NULL CHECK (evidence_hash ~ '^[0-9a-f]{64}$'),
    idempotency_key_hash TEXT NOT NULL CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (id, draft_id, project_id),
    UNIQUE (project_id, draft_id, idempotency_key_hash),
    UNIQUE (project_id, draft_id, dependency_hash, evidence_hash),
    FOREIGN KEY (draft_revision_id, draft_id, project_id)
        REFERENCES app.datastream_setup_draft_revisions(id, draft_id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (proposal_id, draft_id, project_id)
        REFERENCES app.datastream_preconfiguration_proposals(id, draft_id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (observation_id, draft_id, project_id)
        REFERENCES app.datastream_setup_observations(id, draft_id, project_id) ON DELETE RESTRICT
);

CREATE TABLE app.datastream_setup_final_reviews (
    id TEXT PRIMARY KEY CHECK (id ~ '^dsfr_[0-9A-HJKMNP-TV-Z]{26}$'),
    project_id TEXT NOT NULL,
    draft_id TEXT NOT NULL,
    preview_id TEXT NOT NULL,
    review_snapshot JSONB NOT NULL CHECK (app.safe_preconfiguration_evidence(review_snapshot)),
    warning_acknowledgements JSONB NOT NULL CHECK (jsonb_typeof(warning_acknowledgements) = 'array'),
    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (id, draft_id, project_id),
    UNIQUE (project_id, draft_id, content_hash),
    FOREIGN KEY (preview_id, draft_id, project_id)
        REFERENCES app.datastream_setup_previews(id, draft_id, project_id) ON DELETE RESTRICT
);

CREATE TABLE app.datastream_setup_materializations (
    id TEXT PRIMARY KEY CHECK (id ~ '^dsmat_[0-9A-HJKMNP-TV-Z]{26}$'),
    project_id TEXT NOT NULL,
    draft_id TEXT NOT NULL,
    final_review_id TEXT NOT NULL,
    datastream_id TEXT NOT NULL,
    plan_version_id TEXT NOT NULL,
    mapping_version_id TEXT NOT NULL,
    candidate_execution_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, draft_id),
    UNIQUE (project_id, datastream_id),
    UNIQUE (project_id, candidate_execution_id),
    FOREIGN KEY (draft_id, project_id)
        REFERENCES app.datastream_setup_drafts(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (final_review_id, draft_id, project_id)
        REFERENCES app.datastream_setup_final_reviews(id, draft_id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (datastream_id, project_id)
        REFERENCES app.datastreams(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (candidate_execution_id, datastream_id, project_id)
        REFERENCES app.datastream_executions(id, datastream_id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (operation_id) REFERENCES app.operations(id) ON DELETE RESTRICT
);

CREATE OR REPLACE FUNCTION app.reject_datastream_setup_evidence_mutation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$ BEGIN
    RAISE EXCEPTION 'Datastream setup evidence is immutable' USING ERRCODE='23000';
END $$;
CREATE TRIGGER trg_datastream_setup_previews_immutable BEFORE UPDATE OR DELETE
    ON app.datastream_setup_previews FOR EACH ROW
    EXECUTE FUNCTION app.reject_datastream_setup_evidence_mutation();
CREATE TRIGGER trg_datastream_setup_final_reviews_immutable BEFORE UPDATE OR DELETE
    ON app.datastream_setup_final_reviews FOR EACH ROW
    EXECUTE FUNCTION app.reject_datastream_setup_evidence_mutation();
CREATE TRIGGER trg_datastream_setup_materializations_immutable BEFORE UPDATE OR DELETE
    ON app.datastream_setup_materializations FOR EACH ROW
    EXECUTE FUNCTION app.reject_datastream_setup_evidence_mutation();

-- Candidate proof is written only by the activation worker. The relation/object
-- reference must name the execution so a shared or latest object cannot pass.
ALTER TABLE app.datastream_executions ADD COLUMN IF NOT EXISTS artifact_ref TEXT;
ALTER TABLE app.datastream_executions ADD COLUMN IF NOT EXISTS artifact_hash TEXT;
ALTER TABLE app.datastream_executions ADD COLUMN IF NOT EXISTS adapter_ref TEXT;
ALTER TABLE app.datastream_executions ADD COLUMN IF NOT EXISTS candidate_evidence JSONB;
ALTER TABLE app.datastream_executions ADD COLUMN IF NOT EXISTS validated_content_hash TEXT;
ALTER TABLE app.datastream_executions DROP CONSTRAINT IF EXISTS ck_datastream_execution_artifact_scope;
ALTER TABLE app.datastream_executions ADD CONSTRAINT ck_datastream_execution_artifact_scope
    CHECK (artifact_ref IS NULL OR POSITION(id IN artifact_ref) > 0);
ALTER TABLE app.datastream_executions DROP CONSTRAINT IF EXISTS ck_datastream_execution_artifact_hash;
ALTER TABLE app.datastream_executions ADD CONSTRAINT ck_datastream_execution_artifact_hash
    CHECK (artifact_hash IS NULL OR artifact_hash ~ '^[0-9a-f]{64}$');
ALTER TABLE app.datastream_executions DROP CONSTRAINT IF EXISTS ck_datastream_execution_validated_hash;
ALTER TABLE app.datastream_executions ADD CONSTRAINT ck_datastream_execution_validated_hash
    CHECK (validated_content_hash IS NULL OR validated_content_hash ~ '^[0-9a-f]{64}$');
ALTER TABLE app.datastream_executions DROP CONSTRAINT IF EXISTS ck_datastream_execution_safe_evidence;
ALTER TABLE app.datastream_executions ADD CONSTRAINT ck_datastream_execution_safe_evidence
    CHECK (candidate_evidence IS NULL OR app.safe_preconfiguration_evidence(candidate_evidence));

-- A second job kind in the existing queue subsystem. Provider and warehouse
-- code remains outside core; this table only carries exact opaque scope refs.
CREATE TABLE app.datastream_activation_jobs (
    id TEXT PRIMARY KEY CHECK (id ~ '^dsaj_[0-9A-HJKMNP-TV-Z]{26}$'),
    kind TEXT NOT NULL CHECK (kind IN ('setup_preview','candidate_materialization')),
    project_id TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    draft_id TEXT,
    datastream_id TEXT,
    execution_id TEXT,
    correlation_id TEXT NOT NULL,
    payload JSONB NOT NULL CHECK (app.safe_preconfiguration_evidence(payload)),
    state TEXT NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued','running','done','failed','dead_letter')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    error_code TEXT,
    requested_by TEXT NOT NULL,
    enqueued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    UNIQUE (kind, project_id, correlation_id),
    FOREIGN KEY (draft_id, project_id)
        REFERENCES app.datastream_setup_drafts(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (execution_id, datastream_id, project_id)
        REFERENCES app.datastream_executions(id, datastream_id, project_id) ON DELETE RESTRICT,
    CHECK ((kind='setup_preview' AND draft_id IS NOT NULL AND datastream_id IS NULL
                                  AND execution_id IS NULL)
        OR (kind='candidate_materialization' AND draft_id IS NULL
                 AND datastream_id IS NOT NULL AND execution_id IS NOT NULL))
);
CREATE INDEX idx_datastream_activation_jobs_claim
    ON app.datastream_activation_jobs(state, enqueued_at);

CREATE TABLE app.datastream_arrival_monitors (
    datastream_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    expected_interval_minutes INTEGER NOT NULL CHECK (expected_interval_minutes > 0),
    owner_person_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('inactive','active','paused')),
    activated_at TIMESTAMPTZ,
    PRIMARY KEY (datastream_id, project_id),
    FOREIGN KEY (datastream_id, project_id)
        REFERENCES app.datastreams(id, project_id) ON DELETE RESTRICT
);

COMMIT;
