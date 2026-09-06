-- 320 -- the import ledger may name the CATALOG Template it actually used.
--
-- Migration 213 taught `app.managed_feed_import_ledger.import_contract_id` two
-- contract families and validated each against the table its prefix names:
-- `cic_` (the CSV/Excel parsing contract) and `fst_` (a client-saved
-- file-source Template). `file-source-ingestion.md` binds a Template by TWO
-- paths -- "a reused CATALOG or client-saved Template" -- and the catalog path
-- names its contract `template:<CODE>:<version>` (the immutable pair of
-- `app.import_templates`, `file_source_resolution._catalog_template_producer`).
--
-- MEASURED 2026-08-29 on the deployed QA walk (G9, AI-321): the first import
-- that ever reached the ledger under a catalog Template was refused by 213's
-- trigger -- "import_contract_id template:OFFLINE_OOH_V1:2 names no known
-- contract family (expected cic_ or fst_)" -- so the candidate died and the
-- Datastream never reached ACTIVE. Third door of the day refusing one of the
-- two spellings of one binding (after the resolver and the mapping schema).
--
-- THE THIRD FAMILY, validated like the other two: the reference must name a
-- row of `app.import_templates` by (template_code, version). Nothing else
-- changes -- the two families 213 knew keep their check, and a reference no
-- table backs is still refused by name.
--
-- REPLAYABLE. CREATE OR REPLACE FUNCTION; the trigger of 213 keeps its name.

BEGIN;

CREATE OR REPLACE FUNCTION app.validate_import_ledger_contract_ref()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    referenced BOOLEAN;
    catalog_code TEXT;
    catalog_version INTEGER;
BEGIN
    IF NEW.import_contract_id IS NULL THEN
        RETURN NEW;
    END IF;

    IF NEW.import_contract_id LIKE 'cic\_%' THEN
        SELECT EXISTS (
            SELECT 1 FROM app.csv_excel_import_contracts
            WHERE id = NEW.import_contract_id
        ) INTO referenced;
    ELSIF NEW.import_contract_id LIKE 'fst\_%' THEN
        SELECT EXISTS (
            SELECT 1 FROM app.file_source_templates
            WHERE id = NEW.import_contract_id
        ) INTO referenced;
    ELSIF NEW.import_contract_id ~ '^template:[A-Za-z0-9_-]{1,128}:[0-9]{1,6}$' THEN
        -- 320: the catalog family. `template:<CODE>:<version>` is the reference
        -- `_catalog_template_producer` writes; it must name an immutable
        -- catalog row, exactly as an `fst_` must name a client Template.
        catalog_code := split_part(NEW.import_contract_id, ':', 2);
        catalog_version := split_part(NEW.import_contract_id, ':', 3)::INTEGER;
        SELECT EXISTS (
            SELECT 1 FROM app.import_templates
            WHERE template_code = catalog_code AND version = catalog_version
        ) INTO referenced;
    ELSE
        RAISE EXCEPTION
            'import_contract_id % names no known contract family (expected cic_, fst_ or template:<code>:<version>)',
            NEW.import_contract_id;
    END IF;

    IF NOT referenced THEN
        RAISE EXCEPTION
            'import_contract_id % does not exist in the table its prefix names',
            NEW.import_contract_id;
    END IF;

    RETURN NEW;
END;
$$;

COMMIT;
