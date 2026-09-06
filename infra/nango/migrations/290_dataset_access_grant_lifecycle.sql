-- Make dataset access records distinguish a request from effective BigQuery access.
-- Schema Change Checklist: additive, idempotent, legacy rows remain visible and
-- are explicitly non-effective. No existing grant is upgraded to effective.

BEGIN;

ALTER TABLE app.dataset_access_grants
    ADD COLUMN IF NOT EXISTS lifecycle_state TEXT NOT NULL DEFAULT 'requested',
    ADD COLUMN IF NOT EXISTS dataset_id TEXT NULL,
    ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'roles/bigquery.dataViewer',
    ADD COLUMN IF NOT EXISTS effective_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS last_provider_error TEXT NULL,
    ADD COLUMN IF NOT EXISTS provider_error_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS revocation_state TEXT NULL,
    ADD COLUMN IF NOT EXISTS grant_operation_id TEXT NULL
        REFERENCES app.operations(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS revoke_operation_id TEXT NULL
        REFERENCES app.operations(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS provider_attempt_started_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- Rows created before this migration only recorded local intent. Revoked legacy
-- rows remain revoked history; active legacy rows are requests, never effective.
UPDATE app.dataset_access_grants
SET lifecycle_state = CASE WHEN revoked_at IS NULL THEN 'requested' ELSE 'revoked' END,
    role = 'roles/bigquery.dataViewer',
    effective_at = NULL,
    last_provider_error = NULL,
    provider_error_at = NULL,
    revocation_state = NULL,
    grant_operation_id = NULL,
    revoke_operation_id = NULL,
    provider_attempt_started_at = NULL,
    updated_at = NOW()
WHERE lifecycle_state NOT IN ('requested', 'effective', 'failed', 'revoked')
   OR (revoked_at IS NOT NULL AND lifecycle_state <> 'revoked')
   OR (revoked_at IS NULL AND lifecycle_state = 'revoked');

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'dataset_access_grants_lifecycle_ck'
          AND conrelid = 'app.dataset_access_grants'::regclass
    ) THEN
        ALTER TABLE app.dataset_access_grants
            ADD CONSTRAINT dataset_access_grants_lifecycle_ck CHECK (
                (lifecycle_state = 'requested'
                    AND revoked_at IS NULL AND effective_at IS NULL
                    AND revocation_state IS NULL)
                OR (lifecycle_state = 'effective'
                    AND revoked_at IS NULL AND effective_at IS NOT NULL
                    AND dataset_id IS NOT NULL)
                OR (lifecycle_state = 'failed'
                    AND revoked_at IS NULL AND effective_at IS NULL
                    AND revocation_state IS NULL
                    AND last_provider_error IS NOT NULL AND provider_error_at IS NOT NULL)
                OR (lifecycle_state = 'revoked' AND revoked_at IS NOT NULL
                    AND revocation_state IS NULL)
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'dataset_access_grants_revocation_ck'
          AND conrelid = 'app.dataset_access_grants'::regclass
    ) THEN
        ALTER TABLE app.dataset_access_grants
            ADD CONSTRAINT dataset_access_grants_revocation_ck CHECK (
                revocation_state IS NULL
                OR (lifecycle_state = 'effective'
                    AND revocation_state IN ('requested', 'failed'))
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'dataset_access_grants_provider_error_ck'
          AND conrelid = 'app.dataset_access_grants'::regclass
    ) THEN
        ALTER TABLE app.dataset_access_grants
            ADD CONSTRAINT dataset_access_grants_provider_error_ck CHECK (
                (last_provider_error IS NULL) = (provider_error_at IS NULL)
                AND (
                    lifecycle_state IN ('requested', 'failed')
                    OR (lifecycle_state = 'effective'
                        AND COALESCE(
                            revocation_state IN ('requested', 'failed'), FALSE
                        ))
                    OR last_provider_error IS NULL
                )
                AND (revocation_state IS DISTINCT FROM 'failed'
                    OR last_provider_error IS NOT NULL)
                AND (lifecycle_state <> 'revoked' OR last_provider_error IS NULL)
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'dataset_access_grants_role_ck'
          AND conrelid = 'app.dataset_access_grants'::regclass
    ) THEN
        ALTER TABLE app.dataset_access_grants
            ADD CONSTRAINT dataset_access_grants_role_ck
            CHECK (role = 'roles/bigquery.dataViewer');
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'dataset_access_grants_dataset_ck'
          AND conrelid = 'app.dataset_access_grants'::regclass
    ) THEN
        ALTER TABLE app.dataset_access_grants
            ADD CONSTRAINT dataset_access_grants_dataset_ck CHECK (
                dataset_id IS NULL
                OR (dataset_id LIKE 'org\_%\_marts' ESCAPE '\'
                    AND dataset_id NOT LIKE '%\_raw%' ESCAPE '\'
                    AND dataset_id NOT LIKE 'mirror\_%' ESCAPE '\')
            );
    END IF;
END $$;

COMMIT;
