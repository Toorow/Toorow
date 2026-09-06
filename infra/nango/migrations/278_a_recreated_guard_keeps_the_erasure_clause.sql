-- 278 -- a RECREATED guard drops the erasure clause, and this is the fourth time
--
-- THE DEFECT IS NOT A MISSING CLAUSE. It is that `DROP TRIGGER` +
-- `CREATE TRIGGER` silently discards the WHEN clause a previous migration
-- installed, and nothing in the file that recreates the trigger has any reason to
-- mention erasure. The history of this one trigger says it plainly:
--
--   099  gives `trg_connector_installation_protect` the erasure clause
--        (`099_rgpd_erasure_trigger_guards.sql:39` lists connector_installations).
--   178  recreates the trigger to add transition checks -- clause gone
--        (`178_connector_installation_transition_guard.sql:146-150`).
--   200/209 re-arm the hatch across every append-only guard, derived rather than
--        allowlisted -- clause back.
--   264  measures nine more guards and patches them; connector_installations is
--        NOT among its nine, because at that moment it was correct.
--   267  widens the guard for DOMAIN_PENDING / VERIFYING and recreates the
--        trigger -- clause gone again
--        (`267_an_api_connector_owes_no_domain.sql:204-209`).
--
-- So 267 is the migration that broke it, 178 broke it before, and both were
-- ordinary, correct changes to what the guard CHECKS. Neither author had any
-- reason to think about tenant erasure. That is why the answer is not "be
-- careful": it is the derived conformance guard next to this file, which is what
-- caught it.
--
-- MEASURED 2026-08-17, disposable base rebuilt FROM ZERO, 277 migrations applied,
-- `connector` an ordinary NOSUPERUSER / NOBYPASSRLS role:
--
--   DELETE guards in schema `app` ............................... 145
--   inside the org tree (FK-reachable from app.organizations) ... 117 of them
--   carrying no erasure hatch, in the trigger OR its function ... 1
--     -> app.connector_installations.trg_connector_installation_protect
--
-- ONE TRIGGER. An earlier reading of this same question on a long-lived local
-- cluster reported EIGHT, including four that 264 had already patched. That
-- reading was wrong and is corrected here: the cluster had been grown
-- incrementally across sessions with migrations replayed by hand and out of
-- order, so its triggers reflected no order any deployment will ever run. On a
-- clean replay -- which is what production gets -- the four 264 patched are
-- correct, and exactly one regression exists. Measure on a base built from zero.
--
-- TWO WAYS TO CARRY THE HATCH, and reading only one of them is how a check
-- acquires false teeth. Migration 099 puts a WHEN clause on the TRIGGER; 098 puts
-- the test inside the guard FUNCTION body (`app.org_plan_history_block_mutation`
-- returns early when the flag is on). Both work. A check that greps only
-- `pg_get_triggerdef` reports `org_plan_history`, `first_value_events` and
-- `support_access_ledger` as offenders while their erasure path is open. The
-- assertion below reads BOTH, and so does the conformance test.
--
-- WHAT THIS DOES NOT CHANGE. The guard function is untouched, byte for byte --
-- 099's and 264's reason exactly: it carries its own INSERT/UPDATE semantics
-- (immutable identity, six legal state transitions, closed safe metadata) that
-- transcription could silently break. Only the TRIGGER gets the condition. The
-- flag is set with `SET LOCAL` by `core/org_purge.py`, so it is transaction-scoped
-- and cannot leak into another statement or a pooled session.
--
-- ERASURE. This migration creates no table; it edits a trigger on a table the
-- purge already reaches, which is the whole point of it.
--
-- IDEMPOTENT. A trigger that already carries a WHEN clause is skipped, never
-- rewritten -- this migration must not corrupt an expression it did not write.

BEGIN;

DO $migration$
DECLARE
    -- MEASURED, not guessed: the org tree (FK-reachable from app.organizations)
    -- intersected with the DELETE guards of `app`, keeping those carrying the
    -- hatch in NEITHER the trigger definition NOR the guard function body.
    target_tables CONSTANT text[] := ARRAY[
        'connector_installations'
    ];
    guard CONSTANT text :=
        ' WHEN (current_setting(''app.rgpd_erasure'', true) IS DISTINCT FROM ''on'') ';
    rec record;
    new_def text;
    patched int := 0;
    offenders text;
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

    -- -----------------------------------------------------------------------
    -- The class, asserted rather than assumed -- and this is the half that 264
    -- did not have.
    --
    -- 264 patched a measured list and stopped. Three migrations later the same
    -- defect was back on a different trigger. So this states the CLASS is empty
    -- afterwards: every DELETE guard on a table the org tree reaches carries the
    -- hatch, in its trigger or in its function. A tenth instance introduced
    -- between this file being written and being applied fails the migration
    -- loudly instead of leaving an erasure half blocked.
    --
    -- SCOPE is the FK-reachable closure of `app.organizations` (273 tables), a
    -- strict SUPERSET of the 192 that `core.org_purge.plan_purge` names -- which
    -- matters, because a table reached by an ON DELETE CASCADE is deleted by the
    -- cascade and never appears in the plan. `app.org_plan_history` is exactly
    -- that shape, and it is why the plan-scoped guard never covered it.
    --
    -- 099's exclusions need no special case here: `app.audit_log` -- the durable
    -- trace OF the erasure, which must survive it -- and the reference data
    -- belonging to no tenant are not FK-reachable from an organization, so the
    -- walk does not reach them and cannot demand a hatch they must not have.
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
            'AI-67.5: these DELETE guards block a tenant erasure -- the org tree '
            'reaches their table but they carry the `app.rgpd_erasure` hatch in '
            'neither the trigger nor the guard function: %. If a migration '
            'recreated the trigger, add the table to target_tables above. If the '
            'table genuinely must survive an erasure (as app.audit_log does), it '
            'should not be FK-reachable from app.organizations.', offenders;
    END IF;

    RAISE NOTICE
        'AI-67.5: % trigger(s) given the erasure escape hatch; every DELETE guard '
        'inside the org tree now yields to a flagged erasure.', patched;
END
$migration$;

COMMIT;
