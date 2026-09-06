-- Story 38.8 closure: immutable fingerprints, lifecycle provenance and RLS.
BEGIN;

ALTER TABLE app.inbound_receipts
    ADD COLUMN IF NOT EXISTS receipt_fingerprint TEXT;

-- Historical rows predate canonical attachment digests. Give them a stable
-- one-way legacy fingerprint so the column can become mandatory without
-- pretending those rows have evidence that was never captured.
UPDATE app.inbound_receipts
SET receipt_fingerprint =
    md5(concat_ws('|', id, datastream_id, provider_event_id, channel,
                  COALESCE(recipient_hash, ''), attachment_count::text,
                  COALESCE(total_bytes::text, ''), COALESCE(quarantine_uri, '')))
    || md5(concat_ws('|', 'legacy-v1', id, created_at::text))
WHERE receipt_fingerprint IS NULL;

ALTER TABLE app.inbound_receipts
    ALTER COLUMN receipt_fingerprint SET NOT NULL;

DO $constraints$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_inbrx_receipt_fingerprint'
          AND conrelid = 'app.inbound_receipts'::regclass
    ) THEN
        ALTER TABLE app.inbound_receipts
            ADD CONSTRAINT ck_inbrx_receipt_fingerprint
            CHECK (receipt_fingerprint ~ '^[0-9a-f]{64}$') NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_inbrx_terminal_evidence'
          AND conrelid = 'app.inbound_receipts'::regclass
    ) THEN
        ALTER TABLE app.inbound_receipts
            ADD CONSTRAINT ck_inbrx_terminal_evidence CHECK (
                (state = 'LANDED'
                 AND import_ledger_id IS NOT NULL
                 AND btrim(import_ledger_id) <> ''
                 AND error_code IS NULL AND error_detail IS NULL)
                OR (state IN ('RECEIVED', 'PROCESSING')
                    AND import_ledger_id IS NULL
                    AND error_code IS NULL AND error_detail IS NULL)
                OR (state IN ('REJECTED', 'FAILED')
                    AND import_ledger_id IS NULL)
            ) NOT VALID;
    END IF;
END;
$constraints$;

ALTER TABLE app.inbound_receipts
    VALIDATE CONSTRAINT ck_inbrx_receipt_fingerprint;

CREATE OR REPLACE FUNCTION app.protect_inbound_receipt()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, app
SET row_security = off
AS $trigger$
DECLARE
    provenance_command TEXT;
    provenance_org TEXT;
    provenance_resource JSONB;
    receipt_org TEXT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'inbound_receipts rows may not be deleted'
            USING ERRCODE = '23000';
    END IF;

    SELECT d.org_id INTO receipt_org
    FROM app.datastreams d
    WHERE d.id = NEW.datastream_id;
    IF receipt_org IS NULL THEN
        RAISE EXCEPTION 'inbound receipt datastream organization is unresolved'
            USING ERRCODE = '23000';
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'RECEIVED' THEN
            RAISE EXCEPTION 'inbound receipt must start in RECEIVED'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.operation_id IS NULL THEN
            RAISE EXCEPTION 'inbound receipt operation provenance is required'
                USING ERRCODE = '23000';
        END IF;
        SELECT o.command_type, o.effective_org_id, o.resource_path
        INTO provenance_command, provenance_org, provenance_resource
        FROM app.operations o WHERE o.id = NEW.operation_id;
        IF provenance_command IS DISTINCT FROM 'inbound.receipt.recorded'
           OR provenance_org IS DISTINCT FROM receipt_org
           OR provenance_resource IS NULL
           OR NOT provenance_resource @>
               jsonb_build_array('datastream:' || NEW.datastream_id) THEN
            RAISE EXCEPTION 'inbound receipt record provenance is invalid'
                USING ERRCODE = '23000';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.datastream_id IS DISTINCT FROM OLD.datastream_id
       OR NEW.credential_id IS DISTINCT FROM OLD.credential_id
       OR NEW.channel IS DISTINCT FROM OLD.channel
       OR NEW.provider_event_id IS DISTINCT FROM OLD.provider_event_id
       OR NEW.recipient_hash IS DISTINCT FROM OLD.recipient_hash
       OR NEW.receipt_fingerprint IS DISTINCT FROM OLD.receipt_fingerprint
       OR NEW.attachment_count IS DISTINCT FROM OLD.attachment_count
       OR NEW.total_bytes IS DISTINCT FROM OLD.total_bytes
       OR NEW.quarantine_uri IS DISTINCT FROM OLD.quarantine_uri
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'inbound receipt immutable evidence cannot change'
            USING ERRCODE = '23000';
    END IF;

    IF OLD.state IN ('LANDED', 'REJECTED', 'FAILED') THEN
        RAISE EXCEPTION 'terminal inbound receipt rows are immutable'
            USING ERRCODE = '23000';
    END IF;
    IF NOT (
        (OLD.state = 'RECEIVED'
         AND NEW.state IN ('PROCESSING', 'REJECTED', 'FAILED'))
        OR (OLD.state = 'PROCESSING'
            AND NEW.state IN ('LANDED', 'REJECTED', 'FAILED'))
    ) THEN
        RAISE EXCEPTION 'illegal inbound receipt state transition'
            USING ERRCODE = '23000';
    END IF;
    IF NEW.operation_id IS NULL
       OR NEW.operation_id IS NOT DISTINCT FROM OLD.operation_id THEN
        RAISE EXCEPTION 'receipt transition requires new operation provenance'
            USING ERRCODE = '23000';
    END IF;
    IF NEW.updated_at <= OLD.updated_at THEN
        RAISE EXCEPTION 'receipt transition timestamp must advance'
            USING ERRCODE = '23000';
    END IF;
    IF NEW.state = 'LANDED' AND (
        NEW.import_ledger_id IS NULL OR btrim(NEW.import_ledger_id) = ''
    ) THEN
        RAISE EXCEPTION 'LANDED receipt requires import ledger evidence'
            USING ERRCODE = '23000';
    END IF;
    IF NEW.state <> 'LANDED' AND NEW.import_ledger_id IS NOT NULL THEN
        RAISE EXCEPTION 'non-LANDED receipt cannot carry import ledger evidence'
            USING ERRCODE = '23000';
    END IF;
    IF NEW.state IN ('RECEIVED', 'PROCESSING', 'LANDED')
       AND (NEW.error_code IS NOT NULL OR NEW.error_detail IS NOT NULL) THEN
        RAISE EXCEPTION 'non-error receipt state cannot carry error evidence'
            USING ERRCODE = '23000';
    END IF;

    SELECT o.command_type, o.effective_org_id, o.resource_path
    INTO provenance_command, provenance_org, provenance_resource
    FROM app.operations o WHERE o.id = NEW.operation_id;
    IF provenance_command IS DISTINCT FROM 'inbound.receipt.state_advanced'
       OR provenance_org IS DISTINCT FROM receipt_org
       OR provenance_resource IS NULL
       OR NOT provenance_resource @>
           jsonb_build_array('datastream:' || NEW.datastream_id)
       OR NOT provenance_resource @> jsonb_build_array('receipt:' || NEW.id) THEN
        RAISE EXCEPTION 'inbound receipt transition provenance is invalid'
            USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$trigger$;

DROP TRIGGER IF EXISTS trg_inbound_receipt_protect ON app.inbound_receipts;
CREATE TRIGGER trg_inbound_receipt_protect
    BEFORE INSERT OR UPDATE OR DELETE ON app.inbound_receipts
    FOR EACH ROW EXECUTE FUNCTION app.protect_inbound_receipt();

ALTER TABLE app.inbound_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.inbound_receipts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS inbound_receipts_strict ON app.inbound_receipts;
CREATE POLICY inbound_receipts_strict ON app.inbound_receipts
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR EXISTS (
            SELECT 1 FROM app.datastreams d
            WHERE d.id = datastream_id
              AND app.epic36_has_resource_access(d.org_id, 'flux', d.id)
        )
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR EXISTS (
            SELECT 1 FROM app.datastreams d
            WHERE d.id = datastream_id
              AND app.epic36_has_resource_access(d.org_id, 'flux', d.id)
        )
    );

COMMIT;
