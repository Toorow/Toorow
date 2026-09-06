-- Story 38.9 closure: durable per-attachment evidence and lifecycle guards.
BEGIN;

ALTER TABLE app.inbound_raw_imports
    ADD COLUMN IF NOT EXISTS retention_policy_version TEXT,
    ADD COLUMN IF NOT EXISTS retention_days INT,
    ADD COLUMN IF NOT EXISTS duplicate_of_raw_import_id TEXT,
    ADD COLUMN IF NOT EXISTS duplicate_policy_version TEXT,
    ADD COLUMN IF NOT EXISTS quarantine_deleted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS deletion_operation_id TEXT;

UPDATE app.inbound_raw_imports
SET retention_policy_version = COALESCE(
        retention_policy_version, 'quarantine-retention-v1'
    ),
    retention_days = COALESCE(retention_days, 30)
WHERE retention_policy_version IS NULL OR retention_days IS NULL;

ALTER TABLE app.inbound_raw_imports
    ALTER COLUMN retention_policy_version SET NOT NULL,
    ALTER COLUMN retention_days SET NOT NULL;

DO $constraints$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_inbraw_duplicate_of'
          AND conrelid = 'app.inbound_raw_imports'::regclass
    ) THEN
        ALTER TABLE app.inbound_raw_imports
            ADD CONSTRAINT fk_inbraw_duplicate_of
            FOREIGN KEY (duplicate_of_raw_import_id)
            REFERENCES app.inbound_raw_imports(id) ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_inbraw_deletion_operation'
          AND conrelid = 'app.inbound_raw_imports'::regclass
    ) THEN
        ALTER TABLE app.inbound_raw_imports
            ADD CONSTRAINT fk_inbraw_deletion_operation
            FOREIGN KEY (deletion_operation_id)
            REFERENCES app.operations(id) ON DELETE RESTRICT;
    END IF;
END;
$constraints$;

ALTER TABLE app.inbound_raw_imports
    DROP CONSTRAINT IF EXISTS inbound_raw_imports_state_check;
ALTER TABLE app.inbound_raw_imports
    DROP CONSTRAINT IF EXISTS ck_inbraw_state;
ALTER TABLE app.inbound_raw_imports
    ADD CONSTRAINT ck_inbraw_state CHECK (
        state IN (
            'RECEIVED', 'SCANNING', 'ACCEPTED', 'REJECTED',
            'LANDED', 'FAILED', 'DUPLICATE'
        )
    ) NOT VALID;
ALTER TABLE app.inbound_raw_imports
    VALIDATE CONSTRAINT ck_inbraw_state;

DO $constraints$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_inbraw_lifecycle_evidence'
          AND conrelid = 'app.inbound_raw_imports'::regclass
    ) THEN
        ALTER TABLE app.inbound_raw_imports
            ADD CONSTRAINT ck_inbraw_lifecycle_evidence CHECK (
                (state = 'LANDED'
                 AND import_ledger_id IS NOT NULL
                 AND btrim(import_ledger_id) <> ''
                 AND duplicate_of_raw_import_id IS NULL
                 AND duplicate_policy_version IS NULL
                 AND error_code IS NULL AND error_detail IS NULL)
                OR (state = 'DUPLICATE'
                    AND import_ledger_id IS NULL
                    AND duplicate_of_raw_import_id IS NOT NULL
                    AND duplicate_of_raw_import_id <> id
                    AND duplicate_policy_version = 'skip-exact-v1'
                    AND error_code IS NULL AND error_detail IS NULL)
                OR (state IN (
                        'RECEIVED', 'SCANNING', 'ACCEPTED',
                        'REJECTED', 'FAILED'
                    )
                    AND import_ledger_id IS NULL
                    AND duplicate_of_raw_import_id IS NULL
                    AND duplicate_policy_version IS NULL
                    AND (state IN ('REJECTED', 'FAILED')
                         OR (error_code IS NULL AND error_detail IS NULL)))
            ) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_inbraw_retention_policy'
          AND conrelid = 'app.inbound_raw_imports'::regclass
    ) THEN
        ALTER TABLE app.inbound_raw_imports
            ADD CONSTRAINT ck_inbraw_retention_policy CHECK (
                retention_policy_version = 'quarantine-retention-v1'
                AND retention_days BETWEEN 1 AND 3650
            ) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_inbraw_deletion_evidence'
          AND conrelid = 'app.inbound_raw_imports'::regclass
    ) THEN
        ALTER TABLE app.inbound_raw_imports
            ADD CONSTRAINT ck_inbraw_deletion_evidence CHECK (
                (quarantine_deleted_at IS NULL AND deletion_operation_id IS NULL)
                OR (quarantine_deleted_at IS NOT NULL
                    AND deletion_operation_id IS NOT NULL
                    AND legal_hold = FALSE)
            ) NOT VALID;
    END IF;
END;
$constraints$;

CREATE OR REPLACE FUNCTION app.protect_inbound_raw_import()
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
    raw_org TEXT;
    receipt_datastream TEXT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on' THEN
            RAISE EXCEPTION 'inbound_raw_imports rows may not be deleted'
                USING ERRCODE = '23000';
        END IF;
        RETURN OLD;
    END IF;

    SELECT d.org_id INTO raw_org
    FROM app.datastreams d
    WHERE d.id = NEW.datastream_id;
    IF raw_org IS NULL THEN
        RAISE EXCEPTION 'raw import datastream organization is unresolved'
            USING ERRCODE = '23000';
    END IF;

    SELECT r.datastream_id INTO receipt_datastream
    FROM app.inbound_receipts r
    WHERE r.id = NEW.receipt_id;
    IF receipt_datastream IS DISTINCT FROM NEW.datastream_id THEN
        RAISE EXCEPTION 'raw import receipt and Datastream are inconsistent'
            USING ERRCODE = '23000';
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'RECEIVED' THEN
            RAISE EXCEPTION 'raw import must start in RECEIVED'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.quarantine_uri IS NULL OR btrim(NEW.quarantine_uri) = ''
           OR NEW.retention_expires_at IS NULL
           OR NEW.retention_policy_version IS DISTINCT FROM 'quarantine-retention-v1'
           OR NEW.retention_days NOT BETWEEN 1 AND 3650 THEN
            RAISE EXCEPTION 'raw import requires quarantine and retention evidence'
                USING ERRCODE = '23000';
        END IF;
        IF position(
            '/inbound/' || raw_org || '/' || NEW.datastream_id || '/'
            || NEW.content_hash || '/' IN NEW.quarantine_uri
        ) = 0 THEN
            RAISE EXCEPTION 'raw import quarantine URI is outside its content scope'
                USING ERRCODE = '23000';
        END IF;
        SELECT o.command_type, o.effective_org_id, o.resource_path
        INTO provenance_command, provenance_org, provenance_resource
        FROM app.operations o WHERE o.id = NEW.operation_id;
        IF provenance_command IS DISTINCT FROM 'inbound.raw_import.recorded'
           OR provenance_org IS DISTINCT FROM raw_org
           OR provenance_resource IS NULL
           OR NOT provenance_resource @>
               jsonb_build_array('datastream:' || NEW.datastream_id)
           OR NOT provenance_resource @>
               jsonb_build_array('receipt:' || NEW.receipt_id)
           OR NOT provenance_resource @>
               jsonb_build_array('attachment:' || NEW.ordinal::text) THEN
            RAISE EXCEPTION 'raw import record provenance is invalid'
                USING ERRCODE = '23000';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.receipt_id IS DISTINCT FROM OLD.receipt_id
       OR NEW.datastream_id IS DISTINCT FROM OLD.datastream_id
       OR NEW.ordinal IS DISTINCT FROM OLD.ordinal
       OR NEW.filename IS DISTINCT FROM OLD.filename
       OR NEW.media_type_declared IS DISTINCT FROM OLD.media_type_declared
       OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
       OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
       OR NEW.quarantine_uri IS DISTINCT FROM OLD.quarantine_uri
       OR NEW.retention_expires_at IS DISTINCT FROM OLD.retention_expires_at
       OR NEW.retention_policy_version IS DISTINCT FROM OLD.retention_policy_version
       OR NEW.retention_days IS DISTINCT FROM OLD.retention_days
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'raw import immutable evidence cannot change'
            USING ERRCODE = '23000';
    END IF;

    -- A distinct audited path may update legal hold or record physical deletion
    -- without rewriting the terminal processing outcome.
    IF NEW.state IS NOT DISTINCT FROM OLD.state THEN
        IF NEW.legal_hold IS DISTINCT FROM OLD.legal_hold THEN
            IF OLD.legal_hold OR NOT NEW.legal_hold THEN
                RAISE EXCEPTION 'legal hold release requires a separate governed workflow'
                    USING ERRCODE = '23000';
            END IF;
            SELECT o.command_type, o.effective_org_id, o.resource_path
            INTO provenance_command, provenance_org, provenance_resource
            FROM app.operations o WHERE o.id = NEW.operation_id;
            IF provenance_command IS DISTINCT FROM 'inbound.raw_import.legal_hold_changed'
               OR provenance_org IS DISTINCT FROM raw_org
               OR provenance_resource IS NULL
               OR NOT provenance_resource @>
                   jsonb_build_array('raw_import:' || NEW.id) THEN
                RAISE EXCEPTION 'raw import legal-hold provenance is invalid'
                    USING ERRCODE = '23000';
            END IF;
            RETURN NEW;
        END IF;
        IF NEW.quarantine_deleted_at IS DISTINCT FROM OLD.quarantine_deleted_at
           OR NEW.deletion_operation_id IS DISTINCT FROM OLD.deletion_operation_id THEN
            IF OLD.legal_hold OR NEW.legal_hold
               OR NEW.quarantine_deleted_at IS NULL
               OR NEW.deletion_operation_id IS NULL
               OR NEW.retention_expires_at > clock_timestamp() THEN
                RAISE EXCEPTION 'held or unaudited quarantine deletion refused'
                    USING ERRCODE = '23000';
            END IF;
            SELECT o.command_type, o.effective_org_id, o.resource_path
            INTO provenance_command, provenance_org, provenance_resource
            FROM app.operations o WHERE o.id = NEW.deletion_operation_id;
            IF provenance_command IS DISTINCT FROM 'inbound.raw_import.quarantine_deleted'
               OR provenance_org IS DISTINCT FROM raw_org
               OR provenance_resource IS NULL
               OR NOT provenance_resource @>
                   jsonb_build_array('raw_import:' || NEW.id) THEN
                RAISE EXCEPTION 'raw import deletion provenance is invalid'
                    USING ERRCODE = '23000';
            END IF;
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'raw import update has no permitted lifecycle change'
            USING ERRCODE = '23000';
    END IF;

    IF OLD.state IN ('LANDED', 'REJECTED', 'FAILED', 'DUPLICATE') THEN
        RAISE EXCEPTION 'terminal raw import processing evidence is immutable'
            USING ERRCODE = '23000';
    END IF;
    IF NOT (
        (OLD.state = 'RECEIVED'
         AND NEW.state IN ('SCANNING', 'REJECTED', 'FAILED', 'DUPLICATE'))
        OR (OLD.state = 'SCANNING'
            AND NEW.state IN ('ACCEPTED', 'REJECTED', 'FAILED'))
        OR (OLD.state = 'ACCEPTED'
            AND NEW.state IN ('LANDED', 'REJECTED', 'FAILED', 'DUPLICATE'))
    ) THEN
        RAISE EXCEPTION 'illegal raw import state transition'
            USING ERRCODE = '23000';
    END IF;
    IF NEW.operation_id IS NULL
       OR NEW.operation_id IS NOT DISTINCT FROM OLD.operation_id
       OR NEW.updated_at <= OLD.updated_at THEN
        RAISE EXCEPTION 'raw import transition requires fresh provenance'
            USING ERRCODE = '23000';
    END IF;
    IF NEW.state = 'LANDED' AND (
        NEW.import_ledger_id IS NULL OR btrim(NEW.import_ledger_id) = ''
    ) THEN
        RAISE EXCEPTION 'LANDED raw import requires import ledger evidence'
            USING ERRCODE = '23000';
    END IF;
    IF NEW.state = 'DUPLICATE' AND (
        NEW.duplicate_of_raw_import_id IS NULL
        OR NEW.duplicate_of_raw_import_id = NEW.id
        OR NEW.duplicate_policy_version IS DISTINCT FROM 'skip-exact-v1'
    ) THEN
        RAISE EXCEPTION 'DUPLICATE raw import requires versioned evidence'
            USING ERRCODE = '23000';
    END IF;
    IF NEW.state = 'DUPLICATE' AND NOT EXISTS (
        SELECT 1 FROM app.inbound_raw_imports prior
        WHERE prior.id = NEW.duplicate_of_raw_import_id
          AND prior.datastream_id = NEW.datastream_id
          AND prior.content_hash = NEW.content_hash
          AND prior.state IN ('LANDED', 'DUPLICATE')
    ) THEN
        RAISE EXCEPTION 'DUPLICATE raw import reference is not eligible'
            USING ERRCODE = '23000';
    END IF;

    SELECT o.command_type, o.effective_org_id, o.resource_path
    INTO provenance_command, provenance_org, provenance_resource
    FROM app.operations o WHERE o.id = NEW.operation_id;
    IF provenance_command IS DISTINCT FROM 'inbound.raw_import.state_changed'
       OR provenance_org IS DISTINCT FROM raw_org
       OR provenance_resource IS NULL
       OR NOT provenance_resource @>
           jsonb_build_array('datastream:' || NEW.datastream_id)
       OR NOT provenance_resource @>
           jsonb_build_array('raw_import:' || NEW.id)
       OR NOT provenance_resource @>
           jsonb_build_array('state:' || NEW.state) THEN
        RAISE EXCEPTION 'raw import transition provenance is invalid'
            USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$trigger$;

DROP TRIGGER IF EXISTS trg_inbound_raw_import_protect
    ON app.inbound_raw_imports;
CREATE TRIGGER trg_inbound_raw_import_protect
    BEFORE INSERT OR UPDATE OR DELETE ON app.inbound_raw_imports
    FOR EACH ROW EXECUTE FUNCTION app.protect_inbound_raw_import();

ALTER TABLE app.inbound_raw_imports ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.inbound_raw_imports FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS inbound_raw_imports_strict ON app.inbound_raw_imports;
CREATE POLICY inbound_raw_imports_strict ON app.inbound_raw_imports
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
