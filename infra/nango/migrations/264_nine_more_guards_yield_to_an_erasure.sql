-- 264 -- Nine more DELETE guards yield to a flagged erasure (AI-258).
--
-- WHAT WAS BROKEN, AND IT IS THE SAME THING TWICE. Migration 099 gave fifteen
-- immutability triggers a WHEN clause so they do not fire inside a transaction
-- that flagged itself as an RGPD erasure. Nothing has asked the question since,
-- and migrations kept adding guards. Measured 2026-08-15 against
-- `core.org_purge.plan_purge` intersected with `pg_trigger`:
--
--   tables the purge plan reaches ......................... 192
--   DELETE guards on those tables ......................... 112
--   guards carrying the escape hatch ...................... 103
--   guards that would REFUSE the erasure .................. 9
--
-- Three of the nine sit on tables 099 already handled -- `datastream_plan_versions`,
-- `datastream_mapping_versions`, `datastream_publication_log`. A later migration
-- added a SECOND, immutability trigger beside the one 099 patched, and the new
-- one blocks. That is why the check now lives in the test suite
-- (`test_immutability_triggers_yield_to_erasure`) instead of in anyone's memory:
-- the defect is not that fifteen were missed, it is that the question is asked
-- once per emergency.
--
-- WHAT THIS DOES NOT CHANGE. The guard bodies are untouched, byte for byte --
-- 099's reason exactly: each carries its own UPDATE-side semantics that
-- transcription could silently break. Only the TRIGGER gets the condition, and
-- the flag is set with SET LOCAL by `core/org_purge.py`, so it is
-- transaction-scoped and cannot leak into another statement or a pooled session.
--
-- SCOPE. Every table below is one `plan_purge` itself names, so each is genuinely
-- inside the org tree. 099's two exclusions stand and are NOT touched here:
-- `app.audit_log` is the durable trace OF the erasure and must survive it, and
-- reference data belonging to no tenant has no business being deleted by an org
-- erasure. `file_source_templates` appears below and NOT among those exclusions:
-- 099 called it global, and it is not -- it references `app.projects`, so it is
-- org-owned and the purge plan reaches it.
--
-- ERASURE. NOT `core.org_purge` in the sense the guard checks: this migration
-- creates no table. It edits triggers on tables the purge already reaches, which
-- is the whole point of it.
--
-- Idempotent: a trigger that already carries a WHEN clause is skipped, never
-- rewritten -- this migration must not corrupt an expression it did not write.

BEGIN;

DO $migration$
DECLARE
    -- MEASURED, not guessed: `plan_purge(conn, 'org_EXAMPLE')` intersected with
    -- the DELETE triggers of `app`, keeping those with no `rgpd_erasure` clause.
    target_tables CONSTANT text[] := ARRAY[
        'datastream_inbound_credential_rate_events',
        'datastream_mapping_publication_log',
        'datastream_mapping_versions',
        'datastream_plan_versions',
        'datastream_publication_log',
        'external_bq_observations',
        'file_source_template_confirmations',
        'file_source_templates',
        'inbound_raw_imports'
    ];
    guard CONSTANT text :=
        ' WHEN (current_setting(''app.rgpd_erasure'', true) IS DISTINCT FROM ''on'') ';
    rec record;
    new_def text;
    patched int := 0;
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
        patched := patched + 1;
        RAISE NOTICE 'guarded %.%', rec.tbl, rec.tgname;
    END LOOP;

    -- A migration that patched NOTHING on a fresh base means the names above no
    -- longer match the catalog -- it must say so rather than commit silently.
    -- Zero is legitimate only on a re-run, where every one is already conditional.
    RAISE NOTICE 'AI-258: % trigger(s) given the erasure escape hatch', patched;
END
$migration$;

COMMIT;
