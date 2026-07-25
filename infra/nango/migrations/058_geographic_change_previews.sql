-- Story 37.4: immutable geographic impact previews and governed confirmation.
-- Additive, idempotent, project-scoped. No fact or publication pointer mutation.

BEGIN;

CREATE TABLE IF NOT EXISTS app.geographic_change_previews (
    id                       TEXT        NOT NULL,
    project_id               TEXT        NOT NULL
                             REFERENCES app.projects(id) ON DELETE RESTRICT,
    previous_posture         JSONB       NOT NULL
                             CHECK (jsonb_typeof(previous_posture) = 'object'),
    target_posture           JSONB       NOT NULL
                             CHECK (jsonb_typeof(target_posture) = 'object'),
    dependency_fingerprint   TEXT        NOT NULL
                             CHECK (dependency_fingerprint ~ '^[0-9a-f]{64}$'),
    impact                   JSONB       NOT NULL
                             CHECK (jsonb_typeof(impact) = 'object'),
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
    CONSTRAINT pk_geographic_change_previews PRIMARY KEY (id),
    CONSTRAINT uq_geographic_change_preview_idempotency
        UNIQUE (project_id, idempotency_key_hash),
    CONSTRAINT chk_geographic_change_confirmation
        CHECK (
            (status = 'pending' AND confirmation IS NULL AND confirmed_at IS NULL)
            OR
            (status = 'confirmed' AND confirmation IS NOT NULL AND confirmed_at IS NOT NULL)
            OR
            status = 'superseded'
        )
);

CREATE INDEX IF NOT EXISTS idx_geographic_change_previews_project
    ON app.geographic_change_previews (project_id, created_at DESC);

COMMIT;
