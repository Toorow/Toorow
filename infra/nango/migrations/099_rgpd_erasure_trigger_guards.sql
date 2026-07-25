-- 099_rgpd_erasure_trigger_guards.sql
--
-- Second half of the RGPD org-erasure fix (see 098).
--
-- Running the real deletion surfaced ~15 MORE tables inside the org tree whose
-- DELETE guard refuses the erasure ("inbound_receipts rows may not be deleted",
-- "connector_installations is append-only (no DELETE)", ...). They enforce a
-- genuine invariant -- history is not rewritable by the application -- but a
-- tenant erasure is a different, human-gated, audited operation, and today it
-- is simply impossible.
--
-- Rather than rewriting fifteen trigger function BODIES (each has its own
-- UPDATE-side semantics that transcription could silently break), the guard is
-- left byte-for-byte intact and the TRIGGER is given a WHEN clause: it simply
-- does not fire inside a transaction that flagged itself as an erasure. The
-- flag is set with SET LOCAL by core/org_purge.py, so it is transaction-scoped
-- and cannot leak into another statement, session, or pooled connection.
--
-- Scope is an EXPLICIT allowlist of org-scoped tables, never "every guard":
--   * app.audit_log is deliberately absent -- it is the durable trace OF the
--     erasure and must survive it (098 unpinned its FK instead).
--   * Global reference data (import_templates, file_source_templates,
--     target_field_approvals, context_topics_versions, procedures_versions,
--     schema_context_versions) is absent: it does not belong to any tenant, so
--     no org erasure has any business deleting it.
--
-- Idempotent: re-running rebuilds the same trigger definitions.

BEGIN;

DO $migration$
DECLARE
    -- Org-scoped tables whose DELETE guard must yield to a flagged erasure.
    -- Derived from the actual purge plan (core.org_purge) intersected with the
    -- protective triggers found in pg_trigger -- not from guesswork.
    target_tables CONSTANT text[] := ARRAY[
        'connector_activations',
        'connector_domain_configs',
        'connector_installations',
        'connector_verification_runs',
        'csv_excel_import_contracts',
        'datastream_inbound_credentials',
        'datastream_mapping_versions',
        'datastream_plan_versions',
        'datastream_publication_log',
        'inbound_brand_match_decisions',
        'inbound_receipts',
        'managed_feed_import_ledger',
        'managed_feed_rejected_rows',
        'media_plan_versions',
        'plan_allocation_daily'
    ];
    guard CONSTANT text :=
        ' WHEN (current_setting(''app.rgpd_erasure'', true) IS DISTINCT FROM ''on'') ';
    rec record;
    new_def text;
BEGIN
    FOR rec IN
        SELECT c.relname AS tbl, t.tgname, pg_get_triggerdef(t.oid) AS def
        FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid
        WHERE c.relnamespace = 'app'::regnamespace
          AND NOT t.tgisinternal
          AND (t.tgtype & 8) > 0                     -- fires on DELETE
          AND c.relname = ANY (target_tables)
    LOOP
        IF rec.def ILIKE '% WHEN %' THEN
            -- Already carries a condition (this migration re-run, or a guard
            -- that was always conditional): leave it alone rather than
            -- corrupting an expression this migration did not write.
            RAISE NOTICE 'skip %.% -- already conditional', rec.tbl, rec.tgname;
            CONTINUE;
        END IF;

        -- pg_get_triggerdef always ends with "EXECUTE FUNCTION ...": the WHEN
        -- clause belongs immediately before it.
        new_def := regexp_replace(
            rec.def, '\s+EXECUTE (FUNCTION|PROCEDURE)\s', guard || 'EXECUTE \1 '
        );
        IF new_def = rec.def THEN
            RAISE EXCEPTION 'could not inject WHEN clause into %.%: %',
                rec.tbl, rec.tgname, rec.def;
        END IF;

        EXECUTE format('DROP TRIGGER %I ON app.%I', rec.tgname, rec.tbl);
        EXECUTE new_def;
        RAISE NOTICE 'guarded %.%', rec.tbl, rec.tgname;
    END LOOP;
END
$migration$;

COMMIT;
