-- 339 -- the erasure hatch is DERIVED from the org tree, not typed into a list.
--
-- WHY THIS FILE IS NOT WHAT THE TRIAGE THOUGHT IT WAS. The pre-deployment triage
-- of 2026-09-02 read `test_immutability_triggers_yield_to_erasure.py` failing on
-- six tables -- csv_excel_import_contracts, datastream_mapping_versions,
-- datastream_plan_versions, datastream_publication_log,
-- managed_feed_import_ledger, managed_feed_rejected_rows -- and concluded that an
-- org erasure fails on them in production. MEASURED, and it does not:
--
--   Clean replay 001 -> 338 into a database created FROM ZERO in the same
--   cluster (`toorow_replay`, port 55432, `apply_migrations.py --dsn --target`),
--   probing `pg_get_triggerdef`/`pg_get_functiondef` after 099, 200, 209, 264,
--   277, 278 and 338:
--
--     all six carry the hatch at EVERY one of those points.
--
--   The same probe on the working base `toorow_test` -- the same cluster, the
--   same 338 migrations, applied in order with zero out-of-order rows in
--   `toorow_meta.schema_migrations` -- returns six WITHOUT it.
--
-- The difference between the two databases is not a migration. It is that the
-- test suites have run against one of them.
--
-- THE ACTUAL DEFECT, AND IT IS ALREADY NAMED IN THIS REPOSITORY. Twenty-seven
-- pg-gated test files replay raw migration files into their fixture --
-- `cur.execute(path.read_text())` -- a habit from before the disposable cluster,
-- when the test database was not migrated. `tests/migration_ledger.py` named the
-- class on 2026-08-17 for a different symptom (replaying 076 reinstalled a
-- function body that 103 and 226 had fixed, and cost 113 NotNullViolations in
-- files that had asked for nothing). One file was converted to the helper it
-- wrote. The other twenty-seven were not.
--
-- Migrations 030, 032, 042, 077 and 078 all PREDATE 099, and each opens with
-- `DROP TRIGGER IF EXISTS ... ; CREATE TRIGGER ...`. Replaying one against a live
-- base therefore discards the WHEN clause 099 installed -- the exact mechanism
-- 278 documented for migration 267, except the agent here is a fixture, not a
-- migration. Eight of the twenty-seven files replay that set, and their union is
-- exactly the six tables the guard reports:
--
--   030 -> trg_datastream_plan_versions_immutable
--   032 -> trg_datastream_mapping_versions_immutable
--   042 -> trg_datastream_publication_log_immutable
--   077 -> trg_managed_feed_import_ledger_protect,
--          trg_managed_feed_rejected_rows_immutable
--   078 -> trg_csv_excel_contract_immutable
--
-- So 278 was RIGHT when it measured one offender on a base built from zero, and
-- the triage was right about what it saw. Both readings are of different
-- databases. That is the whole of the divergence, and it is why this file says
-- it out loud instead of letting the next session measure it a fifth time.
--
-- THE REPAIR IS IN THE TESTS, and it lands in the same commit as this file:
-- every one of the twenty-seven now goes through
-- `tests.migration_ledger.apply_migrations_absent_from_the_ledger`, and
-- `tests/conformance/test_a_fixture_does_not_replay_a_migration_file.py` stops
-- the twenty-eighth from being written. Without that, this migration would be
-- green for exactly as long as it takes to run one integration suite.
--
-- SO WHY A MIGRATION AT ALL. Two reasons, and neither is decoration.
--
--   (1) A DAMAGED DATABASE STAYS DAMAGED. Every disposable base a session is
--       holding right now has lost the clause, and so has any database a suite
--       was ever pointed at. `tests-must-not-write-to-prod` records that
--       TEST_POSTGRES_DSN pointed at PRODUCTION until the scrub of 2026-07-27 --
--       long enough for these very chains to run there. 264 (2026-08-15) re-armed
--       three of the six by name; it never listed csv_excel_import_contracts,
--       managed_feed_import_ledger or managed_feed_rejected_rows, because at that
--       moment its own measurement did not see them. If production carries the
--       damage on those three, nothing has repaired it and an erasure fails
--       there. This migration repairs whatever it finds and NAMES it in a NOTICE,
--       so the deploy log answers the question instead of a guess.
--
--   (2) 278 LEFT A LIST TO MAINTAIN. It ends by telling the next author to "add
--       the table to target_tables above" -- a hand-kept allowlist, which is the
--       thing that failed in 099 and again in 264. This file has no list. It
--       derives the set from the FK closure of `app.organizations` intersected
--       with the DELETE guards of `app`, which is the same authority the
--       conformance test reads. A table that enters the org tree tomorrow is
--       covered the day it does, and a guard a future migration recreates is
--       repaired the next time any migration runs.
--
-- SCOPE, and 099's exclusions need no special case. The walk is the FK-reachable
-- closure of `app.organizations`. `app.audit_log` -- the durable trace OF the
-- erasure, which must survive it -- and reference data belonging to no tenant are
-- not FK-reachable from an organization, so the walk cannot demand a hatch they
-- must not have. `test_the_audit_log_deliberately_does_not_yield` pins that.
--
-- TWO WAYS TO CARRY THE HATCH. 099 puts a WHEN clause on the TRIGGER; 098 puts
-- the test inside the guard FUNCTION body, which returns early when the flag is
-- on. Both open the erasure path, and reading only the first is how a check
-- acquires false teeth (`org_plan_history`, `first_value_events`,
-- `support_access_ledger` are that shape). Both are read below.
--
-- WHAT THIS DOES NOT CHANGE. No guard function body is touched, byte for byte --
-- 099's, 264's and 278's reason exactly: each carries its own INSERT/UPDATE
-- semantics that transcription could silently break. Only the TRIGGER gets the
-- condition. The flag is set with `SET LOCAL` by `core/org_purge.py`, so it is
-- transaction-scoped and cannot leak into another statement or a pooled session.
--
-- ERASURE. This migration creates no table; it edits triggers on tables the purge
-- already reaches, which is the whole point of it.
--
-- IDEMPOTENT. A trigger that already carries a WHEN clause is skipped, never
-- rewritten -- this migration must not corrupt an expression it did not write.
-- On a base that was never poisoned it patches ZERO, and that is the expected
-- result, not a symptom: 264's "a migration that patched nothing means the names
-- no longer match" warning applied to an ALLOWLIST. This file has none, so its
-- honest report is the count, whatever the count is.

BEGIN;

DO $migration$
DECLARE
    guard CONSTANT text :=
        ' WHEN (current_setting(''app.rgpd_erasure'', true) IS DISTINCT FROM ''on'') ';
    rec record;
    new_def text;
    patched int := 0;
    seen int := 0;
    offenders text;
BEGIN
    -- DERIVED, not listed: every DELETE guard sitting on a table the org tree
    -- reaches, which carries the hatch in NEITHER the trigger definition NOR the
    -- guard function body.
    FOR rec IN
        WITH RECURSIVE tree AS (
            SELECT 'app.organizations'::regclass AS oid
            UNION
            SELECT k.conrelid
            FROM pg_constraint k
            JOIN tree ON k.confrelid = tree.oid
            WHERE k.contype = 'f'
              AND k.conrelid <> k.confrelid
        )
        SELECT c.relname AS tbl, t.tgname, pg_get_triggerdef(t.oid) AS def
        FROM tree
        JOIN pg_class c ON c.oid = tree.oid
        JOIN pg_trigger t ON t.tgrelid = c.oid
        WHERE c.relnamespace = 'app'::regnamespace
          AND NOT t.tgisinternal
          AND (t.tgtype & 8) > 0                     -- fires on DELETE
          AND pg_get_triggerdef(t.oid) NOT ILIKE '%rgpd_erasure%'
          AND pg_get_functiondef(t.tgfoid) NOT ILIKE '%rgpd_erasure%'
        ORDER BY c.relname, t.tgname
    LOOP
        seen := seen + 1;

        IF rec.def ILIKE '% WHEN %' THEN
            -- Conditional on something ELSE. Injecting a second WHEN would
            -- produce invalid SQL, and rewriting the existing expression would
            -- edit a condition this migration did not write. Refuse loudly.
            RAISE EXCEPTION
                'app.%.% already carries a WHEN clause that is not the erasure '
                'hatch; it needs a hand-written condition, not this loop: %',
                rec.tbl, rec.tgname, rec.def;
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
        RAISE NOTICE 'AI-258: guarded app.%.% -- it would have refused an org '
                     'erasure', rec.tbl, rec.tgname;
    END LOOP;

    -- -----------------------------------------------------------------------
    -- The class, asserted rather than assumed -- 278's half, kept, because a
    -- derived patch loop still deserves a derived proof that it left nothing.
    -- Re-read from the catalog AFTER the loop, so this checks the result and not
    -- the intention.
    -- -----------------------------------------------------------------------
    WITH RECURSIVE tree AS (
        SELECT 'app.organizations'::regclass AS oid
        UNION
        SELECT k.conrelid
        FROM pg_constraint k
        JOIN tree ON k.confrelid = tree.oid
        WHERE k.contype = 'f'
          AND k.conrelid <> k.confrelid
    )
    SELECT string_agg(format('app.%s.%s', c.relname, t.tgname), ', ' ORDER BY c.relname)
      INTO offenders
    FROM tree
    JOIN pg_class c ON c.oid = tree.oid
    JOIN pg_trigger t ON t.tgrelid = c.oid
    WHERE c.relnamespace = 'app'::regnamespace
      AND NOT t.tgisinternal
      AND (t.tgtype & 8) > 0
      AND pg_get_triggerdef(t.oid) NOT ILIKE '%rgpd_erasure%'
      AND pg_get_functiondef(t.tgfoid) NOT ILIKE '%rgpd_erasure%';

    IF offenders IS NOT NULL THEN
        RAISE EXCEPTION
            'AI-258: these DELETE guards still block a tenant erasure after the '
            'derived repair ran: %. The loop above should have reached them -- if '
            'it did not, the FK closure and the patch predicate have drifted '
            'apart and that is the defect to fix.', offenders;
    END IF;

    IF patched = 0 THEN
        RAISE NOTICE
            'AI-258: nothing to repair -- every DELETE guard inside the org tree '
            'already yields to a flagged erasure. This is the expected result on '
            'a database that only ever received migrations in order.';
    ELSE
        RAISE NOTICE
            'AI-258: % of % offending DELETE guard(s) repaired. A count above zero '
            'means this database had a migration file replayed into it out of '
            'sequence -- see the header, and see '
            'tests/conformance/test_a_fixture_does_not_replay_a_migration_file.py',
            patched, seen;
    END IF;
END
$migration$;

COMMIT;
