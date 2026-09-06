-- Story 38.11: immutable human confirmation for one exact parser contract.
BEGIN;

CREATE TABLE IF NOT EXISTS app.csv_excel_import_contract_confirmations (
    contract_id   TEXT        NOT NULL PRIMARY KEY
        REFERENCES app.csv_excel_import_contracts(id) ON DELETE RESTRICT,
    datastream_id TEXT        NOT NULL,
    project_id    TEXT        NOT NULL,
    fingerprint   TEXT        NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    confirmed_by  TEXT        NOT NULL CHECK (btrim(confirmed_by) <> ''),
    confirmed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_csv_excel_confirmation_scope
        FOREIGN KEY (datastream_id, project_id)
        REFERENCES app.datastreams(id, project_id) ON DELETE RESTRICT
);

CREATE OR REPLACE FUNCTION app.validate_csv_excel_contract_confirmation()
RETURNS TRIGGER
LANGUAGE plpgsql AS
$$
DECLARE
    contract_row RECORD;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'csv_excel_import_contract_confirmations is append-only';
    END IF;
    SELECT datastream_id, project_id, fingerprint
      INTO contract_row
      FROM app.csv_excel_import_contracts
     WHERE id = NEW.contract_id;
    IF contract_row.datastream_id IS DISTINCT FROM NEW.datastream_id
       OR contract_row.project_id IS DISTINCT FROM NEW.project_id
       OR contract_row.fingerprint IS DISTINCT FROM NEW.fingerprint
    THEN
        RAISE EXCEPTION 'parser contract confirmation is stale or cross-scoped';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_csv_excel_contract_confirmation_immutable
    ON app.csv_excel_import_contract_confirmations;
CREATE TRIGGER trg_csv_excel_contract_confirmation_immutable
    BEFORE INSERT OR UPDATE OR DELETE
    ON app.csv_excel_import_contract_confirmations
    FOR EACH ROW
    EXECUTE FUNCTION app.validate_csv_excel_contract_confirmation();

-- Migration 078 predates the strict tenant RLS floor. The confirmation path now
-- reads that table unattended, so close the whole contract evidence class here.
ALTER TABLE app.csv_excel_import_contracts ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.csv_excel_import_contracts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS csv_excel_import_contracts_strict
    ON app.csv_excel_import_contracts;
CREATE POLICY csv_excel_import_contracts_strict
    ON app.csv_excel_import_contracts
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR EXISTS (
            SELECT 1
              FROM app.datastreams d
             WHERE d.id = csv_excel_import_contracts.datastream_id
               AND d.project_id = csv_excel_import_contracts.project_id
               AND app.epic36_has_resource_access(d.org_id, 'flux', d.id)
        )
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR EXISTS (
            SELECT 1
              FROM app.datastreams d
             WHERE d.id = csv_excel_import_contracts.datastream_id
               AND d.project_id = csv_excel_import_contracts.project_id
               AND app.epic36_has_resource_access(d.org_id, 'flux', d.id)
        )
    );

ALTER TABLE app.csv_excel_import_contract_confirmations ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.csv_excel_import_contract_confirmations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS csv_excel_contract_confirmations_strict
    ON app.csv_excel_import_contract_confirmations;
CREATE POLICY csv_excel_contract_confirmations_strict
    ON app.csv_excel_import_contract_confirmations
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR EXISTS (
            SELECT 1
              FROM app.datastreams d
             WHERE d.id = csv_excel_import_contract_confirmations.datastream_id
               AND d.project_id = csv_excel_import_contract_confirmations.project_id
               AND app.epic36_has_resource_access(d.org_id, 'flux', d.id)
        )
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR EXISTS (
            SELECT 1
              FROM app.datastreams d
             WHERE d.id = csv_excel_import_contract_confirmations.datastream_id
               AND d.project_id = csv_excel_import_contract_confirmations.project_id
               AND app.epic36_has_resource_access(d.org_id, 'flux', d.id)
        )
    );

COMMENT ON TABLE app.csv_excel_import_contract_confirmations IS
    'Immutable project-scoped proof that an operator confirmed one exact parser contract fingerprint; RLS follows Datastream flux access.';

COMMIT;
