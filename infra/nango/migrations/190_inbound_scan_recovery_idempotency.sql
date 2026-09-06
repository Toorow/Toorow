-- Story 38.10: durable idempotency for authorized scan-job recovery.
BEGIN;

ALTER TABLE app.inbound_scan_job_recoveries
    ADD COLUMN IF NOT EXISTS idempotency_key_hash TEXT;

ALTER TABLE app.inbound_scan_job_recoveries
    DROP CONSTRAINT IF EXISTS ck_inbound_scan_recovery_idempotency_hash;
ALTER TABLE app.inbound_scan_job_recoveries
    ADD CONSTRAINT ck_inbound_scan_recovery_idempotency_hash CHECK (
        idempotency_key_hash IS NULL
        OR idempotency_key_hash ~ '^[0-9a-f]{64}$'
    );

ALTER TABLE app.inbound_scan_job_recoveries
    DROP CONSTRAINT IF EXISTS ck_inbound_scan_recovery_idempotency_required;
ALTER TABLE app.inbound_scan_job_recoveries
    ADD CONSTRAINT ck_inbound_scan_recovery_idempotency_required
    CHECK (idempotency_key_hash IS NOT NULL) NOT VALID;
CREATE UNIQUE INDEX IF NOT EXISTS uq_inbound_scan_recovery_idempotency
    ON app.inbound_scan_job_recoveries (job_id, idempotency_key_hash)
    WHERE idempotency_key_hash IS NOT NULL;

COMMIT;