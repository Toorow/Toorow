-- Story 38.6 corrective guard: future template versions are complete contracts.
-- Existing rows remain readable; NOT VALID still enforces the check on new rows.

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'ck_import_templates_contract_shape'
           AND conrelid = 'app.import_templates'::regclass
    ) THEN
        ALTER TABLE app.import_templates
            ADD CONSTRAINT ck_import_templates_contract_shape CHECK (
                jsonb_typeof(contract) = 'object'
                AND contract ?& ARRAY[
                    'required_fields', 'optional_fields', 'aliases', 'field_types',
                    'grain', 'identity_keys', 'date_timezone', 'currency_unit',
                    'sensitive_classification', 'validation', 'canonical_bindings'
                ]
                AND jsonb_typeof(contract->'required_fields') = 'array'
                AND jsonb_typeof(contract->'optional_fields') = 'array'
                AND jsonb_typeof(contract->'aliases') = 'object'
                AND jsonb_typeof(contract->'field_types') = 'object'
                AND jsonb_typeof(contract->'identity_keys') = 'array'
                AND jsonb_typeof(contract->'date_timezone') = 'object'
                AND jsonb_typeof(contract->'currency_unit') = 'object'
                AND jsonb_typeof(contract->'validation') = 'object'
                AND jsonb_typeof(contract->'canonical_bindings') = 'object'
                AND jsonb_typeof(contract->'sensitive_classification') = 'string'
                AND jsonb_typeof(contract->'grain') = 'string'
            ) NOT VALID;
    END IF;
END;
$$;

COMMENT ON CONSTRAINT ck_import_templates_contract_shape
    ON app.import_templates IS
    'Story 38.6: future immutable versions carry the complete registry contract shape.';

COMMIT;
