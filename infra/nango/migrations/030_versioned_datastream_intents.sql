-- Story 12.2: immutable, versioned Datastream intent and schedule policy.
-- Additive after migrations 001-029. Existing Story 8.2 rows remain legacy rows
-- while source_kind and current_plan_version_id are NULL.

BEGIN;

ALTER TABLE app.datastreams
    ADD COLUMN IF NOT EXISTS source_kind TEXT,
    ADD COLUMN IF NOT EXISTS current_plan_version_id TEXT,
    ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS archived_by TEXT;

-- Non-connector Datastreams must not invent a connector module sentinel.
ALTER TABLE app.datastreams ALTER COLUMN module_name DROP NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_datastreams_source_kind'
          AND conrelid = 'app.datastreams'::regclass
    ) THEN
        ALTER TABLE app.datastreams
            ADD CONSTRAINT ck_datastreams_source_kind CHECK (
                source_kind IS NULL
                OR (source_kind = 'connector_pull' AND module_name IS NOT NULL)
                OR (source_kind = 'external_bq' AND module_name IS NULL AND connection_ref_id IS NULL)
                OR (source_kind = 'managed_feed' AND module_name IS NULL)
            );
    END IF;
END $$;

-- Referenced by composite foreign keys to guarantee project ownership.
CREATE UNIQUE INDEX IF NOT EXISTS uq_datastreams_id_project
    ON app.datastreams (id, project_id);

CREATE TABLE IF NOT EXISTS app.datastream_plan_versions (
    id                          TEXT        NOT NULL,
    datastream_id               TEXT        NOT NULL,
    project_id                  TEXT        NOT NULL,
    version_number              INTEGER     NOT NULL CHECK (version_number >= 1),
    contract_version            TEXT        NOT NULL,
    source_kind                 TEXT        NOT NULL
                                CHECK (source_kind IN ('connector_pull', 'external_bq', 'managed_feed')),
    writer_kind                 TEXT        NOT NULL
                                CHECK (writer_kind IN ('toorow', 'external')),
    destination_policy          TEXT        NOT NULL
                                CHECK (destination_policy IN ('managed_raw', 'external_read_only')),
    normalized_payload          JSONB       NOT NULL CHECK (jsonb_typeof(normalized_payload) = 'object'),
    content_hash                TEXT        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    capability_contract_version TEXT,
    capability_fingerprint      TEXT CHECK (
                                    capability_fingerprint IS NULL
                                    OR capability_fingerprint ~ '^[0-9a-f]{64}$'
                                ),
    executable                  BOOLEAN     NOT NULL DEFAULT FALSE,
    validation_issues           JSONB       NOT NULL DEFAULT '[]'::jsonb
                                CHECK (jsonb_typeof(validation_issues) = 'array'),
    idempotency_key_hash        TEXT        NOT NULL
                                CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    created_by                  TEXT        NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_datastream_plan_versions PRIMARY KEY (id),
    CONSTRAINT uq_datastream_plan_version UNIQUE (datastream_id, version_number),
    CONSTRAINT uq_datastream_plan_idempotency UNIQUE (project_id, idempotency_key_hash),
    CONSTRAINT uq_datastream_plan_scope UNIQUE (id, datastream_id, project_id),
    CONSTRAINT fk_datastream_plan_datastream_scope
        FOREIGN KEY (datastream_id, project_id)
        REFERENCES app.datastreams (id, project_id) ON DELETE RESTRICT,
    CONSTRAINT ck_datastream_plan_ownership CHECK (
        (source_kind IN ('connector_pull', 'managed_feed')
         AND writer_kind = 'toorow' AND destination_policy = 'managed_raw')
        OR
        (source_kind = 'external_bq'
         AND writer_kind = 'external' AND destination_policy = 'external_read_only')
    )
);

CREATE INDEX IF NOT EXISTS idx_datastream_plan_versions_project
    ON app.datastream_plan_versions (project_id, datastream_id, version_number DESC);

CREATE TABLE IF NOT EXISTS app.datastream_schedule_state (
    plan_version_id             TEXT        NOT NULL,
    datastream_id               TEXT        NOT NULL,
    project_id                  TEXT        NOT NULL,
    scheduled_for               TIMESTAMPTZ,
    window_start                TIMESTAMPTZ,
    window_end                  TIMESTAMPTZ,
    next_run_at                 TIMESTAMPTZ,
    last_committed_watermark    TIMESTAMPTZ,
    retry_count                 INTEGER     NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    missed_run_count            INTEGER     NOT NULL DEFAULT 0 CHECK (missed_run_count >= 0),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_datastream_schedule_state PRIMARY KEY (plan_version_id),
    CONSTRAINT fk_datastream_schedule_plan_scope
        FOREIGN KEY (plan_version_id, datastream_id, project_id)
        REFERENCES app.datastream_plan_versions (id, datastream_id, project_id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_datastream_schedule_window CHECK (
        (window_start IS NULL AND window_end IS NULL)
        OR (window_start IS NOT NULL AND window_end IS NOT NULL AND window_start < window_end)
    )
);

CREATE INDEX IF NOT EXISTS idx_datastream_schedule_due
    ON app.datastream_schedule_state (next_run_at)
    WHERE next_run_at IS NOT NULL;

-- The current pointer must reference a version owned by the same project and Datastream.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_datastreams_current_plan_scope'
          AND conrelid = 'app.datastreams'::regclass
    ) THEN
        ALTER TABLE app.datastreams
            ADD CONSTRAINT fk_datastreams_current_plan_scope
            FOREIGN KEY (current_plan_version_id, id, project_id)
            REFERENCES app.datastream_plan_versions (id, datastream_id, project_id)
            DEFERRABLE INITIALLY IMMEDIATE;
    END IF;
END $$;

CREATE OR REPLACE FUNCTION app.reject_datastream_plan_version_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'datastream plan versions are immutable'
        USING ERRCODE = '23000';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_datastream_plan_versions_immutable
    ON app.datastream_plan_versions;
CREATE TRIGGER trg_datastream_plan_versions_immutable
    BEFORE UPDATE OR DELETE ON app.datastream_plan_versions
    FOR EACH ROW EXECUTE FUNCTION app.reject_datastream_plan_version_mutation();

COMMIT;
