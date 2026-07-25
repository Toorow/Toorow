-- Story 36.2: canonical operation, normalized audit evidence and generic outbox.
-- Additive/replayable; migration 042's publication-specific outbox remains intact.

BEGIN;

ALTER TABLE app.audit_log ADD COLUMN IF NOT EXISTS operation_id TEXT;
ALTER TABLE app.audit_log ADD COLUMN IF NOT EXISTS effective_org_id TEXT;
ALTER TABLE app.audit_log ADD COLUMN IF NOT EXISTS resource_path JSONB;
ALTER TABLE app.audit_log ADD COLUMN IF NOT EXISTS host_context JSONB;
ALTER TABLE app.audit_log ADD COLUMN IF NOT EXISTS policy_version TEXT;
ALTER TABLE app.audit_log ADD COLUMN IF NOT EXISTS catalog_version TEXT;
ALTER TABLE app.audit_log ADD COLUMN IF NOT EXISTS tool_version TEXT;
ALTER TABLE app.audit_log ADD COLUMN IF NOT EXISTS before_hash TEXT;
ALTER TABLE app.audit_log ADD COLUMN IF NOT EXISTS after_hash TEXT;
ALTER TABLE app.audit_log ADD COLUMN IF NOT EXISTS confirmation_reference_hash TEXT;
ALTER TABLE app.audit_log ADD COLUMN IF NOT EXISTS idempotency_key_hash TEXT;
ALTER TABLE app.audit_log ADD COLUMN IF NOT EXISTS outcome TEXT;
ALTER TABLE app.audit_log ADD COLUMN IF NOT EXISTS trace_id TEXT;

CREATE TABLE IF NOT EXISTS app.operations (
    id                          TEXT        PRIMARY KEY,
    effective_org_id            TEXT        NOT NULL
                                REFERENCES app.organizations(id) ON DELETE RESTRICT,
    command_type                TEXT        NOT NULL,
    actor                       TEXT        NOT NULL,
    resource_path               JSONB       NOT NULL
                                CHECK (jsonb_typeof(resource_path) = 'array'),
    host_context                JSONB       NOT NULL
                                CHECK (jsonb_typeof(host_context) = 'object'),
    versions                    JSONB       NOT NULL
                                CHECK (jsonb_typeof(versions) = 'object'),
    request_hash                TEXT        NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    provider_references         JSONB       NOT NULL DEFAULT '{}'::jsonb
                                CHECK (jsonb_typeof(provider_references) = 'object'),
    confirmation_mode           TEXT        NOT NULL
                                CHECK (confirmation_mode IN ('none', 'server', 'host', 'human')),
    confirmation_reference_hash TEXT        CHECK (
                                    confirmation_reference_hash IS NULL
                                    OR confirmation_reference_hash ~ '^[0-9a-f]{64}$'
                                ),
    idempotency_key_hash        TEXT        NOT NULL
                                CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    trace_id                    TEXT,
    state                       TEXT        NOT NULL DEFAULT 'pending'
                                CHECK (state IN (
                                    'pending', 'succeeded', 'failed', 'outcome_unknown'
                                )),
    outcome                     TEXT,
    before_hash                 TEXT        CHECK (
                                    before_hash IS NULL OR before_hash ~ '^[0-9a-f]{64}$'
                                ),
    after_hash                  TEXT        CHECK (
                                    after_hash IS NULL OR after_hash ~ '^[0-9a-f]{64}$'
                                ),
    result                      JSONB       CHECK (
                                    result IS NULL OR jsonb_typeof(result) = 'object'
                                ),
    audit_event_id              TEXT,
    outbox_event_id             TEXT,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at                TIMESTAMPTZ,
    CONSTRAINT uq_operations_idempotency
        UNIQUE (effective_org_id, command_type, idempotency_key_hash)
);

CREATE INDEX IF NOT EXISTS operations_org_created
    ON app.operations (effective_org_id, created_at DESC);
CREATE INDEX IF NOT EXISTS operations_outcome_unknown
    ON app.operations (updated_at)
    WHERE state = 'outcome_unknown';

CREATE TABLE IF NOT EXISTS app.operation_outbox (
    id                  TEXT        PRIMARY KEY,
    operation_id        TEXT        NOT NULL
                        REFERENCES app.operations(id) ON DELETE RESTRICT,
    event_type          TEXT        NOT NULL,
    payload             JSONB       NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    state               TEXT        NOT NULL DEFAULT 'pending'
                        CHECK (state IN ('pending', 'processing', 'delivered')),
    attempts            INTEGER     NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    locked_by           TEXT,
    locked_at           TIMESTAMPTZ,
    retry_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    delivered_at        TIMESTAMPTZ,
    last_error_class    TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_operation_outbox_event UNIQUE (operation_id, event_type)
);

CREATE INDEX IF NOT EXISTS operation_outbox_pending
    ON app.operation_outbox (retry_at, created_at)
    WHERE state = 'pending';

CREATE INDEX IF NOT EXISTS audit_log_operation_id
    ON app.audit_log (operation_id)
    WHERE operation_id IS NOT NULL;


DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'audit_log_resource_path_json_check'
    ) THEN
        ALTER TABLE app.audit_log ADD CONSTRAINT audit_log_resource_path_json_check
            CHECK (resource_path IS NULL OR jsonb_typeof(resource_path) = 'array');
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'audit_log_host_context_json_check'
    ) THEN
        ALTER TABLE app.audit_log ADD CONSTRAINT audit_log_host_context_json_check
            CHECK (host_context IS NULL OR jsonb_typeof(host_context) = 'object');
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_operations_audit_event'
    ) THEN
        ALTER TABLE app.operations ADD CONSTRAINT fk_operations_audit_event
            FOREIGN KEY (audit_event_id) REFERENCES app.audit_log(id)
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_operations_outbox_event'
    ) THEN
        ALTER TABLE app.operations ADD CONSTRAINT fk_operations_outbox_event
            FOREIGN KEY (outbox_event_id) REFERENCES app.operation_outbox(id)
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
END $$;

COMMIT;
