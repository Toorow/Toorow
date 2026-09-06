-- 277 -- the erasure hatch is a PRIVILEGE too, and two ledgers still lack it
--
-- THE DEFECT, IN ONE SENTENCE: a trigger that says "an RGPD erasure may delete
-- from me" is worth nothing while the role holds no DELETE privilege on the
-- table, because PostgreSQL checks the privilege BEFORE the trigger runs. The
-- statement is refused with 42501 and the trigger that would have allowed it is
-- never reached. Migration 198 wrote that sentence for `org_plan_history`. This
-- migration states the FINAL privilege posture in one place, and repairs the
-- second table where the same hole is open today.
--
-- WHY 198 DID NOT HOLD. `056_org_plan_entitlements.sql:95-101` revokes
-- UPDATE, DELETE from `connector`, and its own header advertises that the REVOKE
-- is wrapped "so the migration is replayable". 198 then granted DELETE back, and
-- 207 restated that grant. Nothing states the two halves together, so the truth
-- depends on which files ran last:
--
--   * a FULL replay in identifier order ends on 207 -> DELETE present.
--   * anything that replays 056 ALONE -> DELETE gone, while 198 stays inscribed
--     in `toorow_meta.schema_migrations`, so the ledger says "applied" and the
--     database disagrees.
--
-- That second case was not hypothetical: `server/tests/core/test_org_entitlements.py`
-- re-executed 056 at the top of every test. `known-debt.json` (2026-08-03)
-- measured it -- privilege true right after `apply_migrations.py`, false after a
-- run of `server/tests/core`. That particular replay is closed (the helper at
-- `test_org_entitlements.py:109-141` now CHECKS instead of replaying), but the
-- file is still replayable and nothing asserts the effective privilege.
--
-- WHAT THIS MIGRATION CAN AND CANNOT PROMISE. It cannot make a REVOKE issued
-- after it harmless -- no migration can. What it does is (a) put both halves of
-- the posture in one reviewable place ABOVE every file that revokes on these
-- tables (056, 090, 183, 207), so the full replay ends here, and (b) hand the
-- residual case to a test instead of to memory:
-- `server/tests/conformance/test_the_erasure_hatch_is_a_privilege_too.py`
-- asserts the EFFECTIVE privilege, so a future revoke is caught rather than
-- suffered. That is the split migration 264 already made for the trigger side of
-- the same question.
--
-- MEASURED 2026-08-17, disposable base at 275 migrations, `connector` an ordinary
-- NOSUPERUSER / NOBYPASSRLS role, tables owned by `postgres`:
--
--   DELETE guards in `app` carrying the `rgpd_erasure` clause ....... 126
--   of those, tables where `connector` holds no DELETE .............. 1
--     -> app.inbound_brand_match_decisions
--   198's own query (ON DELETE CASCADE children of app.organizations
--   lacking DELETE for `connector`), re-run today .................... 1
--     -> app.inbound_brand_match_decisions
--   has_table_privilege('connector','app.org_plan_history','UPDATE') . TRUE
--
-- TWO FINDINGS, and both are the same class as 198's.
--
--   1. `app.inbound_brand_match_decisions` is an ON DELETE CASCADE child of
--      `app.organizations` AND its guard
--      `trg_inbound_brand_match_decisions_protect` already carries the
--      `app.rgpd_erasure` WHEN clause -- a migration decided the erasure must
--      pass. But 090:233 and 207:121 revoke DELETE from `PUBLIC, connector`, so
--      it cannot. Exactly 198's finding, one table over, and 198's query names
--      it because it is now the ONLY one left.
--
--   2. `app.org_plan_history` holds UPDATE today. 056 declared it revoked; 207's
--      blanket `GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA app`
--      handed it back, and 207 restated the narrow posture for `audit_log`,
--      `metric_semantics_audit` and `inbound_brand_match_decisions` -- but for
--      `org_plan_history` it restated only the GRANT half (207:117-118). The
--      trigger still refuses the UPDATE, so nothing was rewritable; the DECLARED
--      posture was simply half gone. This restores it.
--
-- NOT A LOOSENING, and it is 198's own argument (198:36-40): the DELETE guard on
-- both tables keeps enforcing the flag, so these grants open no plain DELETE
-- path. They only let a purge that flagged itself with `app.rgpd_erasure` reach
-- the trigger that judges it. UPDATE stays revoked on both: append-only means
-- history is not REWRITABLE, and erasing a whole tenant is a different, audited
-- operation.
--
-- ERASURE. This migration creates no table, so it owes no erasure claim of its
-- own; it is about the erasure of tables that already exist.
--
-- REPLAYABLE. The whole body is wrapped in the `pg_roles` check migrations 049
-- and 056 use, so it is a no-op on a throwaway base where `connector` does not
-- exist rather than aborting with `undefined_object`.

BEGIN;

DO $migration$
DECLARE
    offenders text;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'connector') THEN
        RAISE NOTICE
            '277: role `connector` does not exist on this cluster -- nothing to '
            'grant or revoke. The append-only TRIGGERS are the enforcement and '
            'are unaffected.';
        RETURN;
    END IF;

    -- -----------------------------------------------------------------------
    -- 1. app.org_plan_history -- the final state 056 / 198 / 207 never stated
    --    in one place. GRANT DELETE (the RGPD hatch of 098/198),
    --    REVOKE UPDATE (the append-only half 056 declared).
    -- -----------------------------------------------------------------------
    EXECUTE 'GRANT DELETE ON app.org_plan_history TO connector';
    EXECUTE 'REVOKE UPDATE ON app.org_plan_history FROM PUBLIC, connector';

    -- -----------------------------------------------------------------------
    -- 2. app.inbound_brand_match_decisions -- same defect, second instance.
    --    Its trigger already yields to a flagged erasure; the privilege did not.
    --    UPDATE stays revoked from everyone, exactly as 090 and 207 declared.
    -- -----------------------------------------------------------------------
    EXECUTE 'GRANT DELETE ON app.inbound_brand_match_decisions TO connector';
    EXECUTE 'REVOKE UPDATE ON app.inbound_brand_match_decisions FROM PUBLIC, connector';

    -- -----------------------------------------------------------------------
    -- 3. The class, asserted rather than assumed.
    --
    --    Naming two tables repairs two tables. This says the CLASS is empty
    --    afterwards, so a third instance -- one that exists on the cluster this
    --    runs against and that the measurement above did not see -- fails the
    --    migration loudly instead of leaving the erasure half open in silence.
    --
    --    Two scopes, unioned, because neither alone catches both findings:
    --
    --      (a) a DELETE guard carrying the `rgpd_erasure` clause. A migration
    --          put that clause there; it DECIDED the erasure must pass.
    --      (b) an ON DELETE CASCADE child of `app.organizations`. 198's own
    --          query. These are reached by the cascade, NOT by an operation
    --          `core.org_purge.plan_purge` names -- which is precisely why the
    --          trigger-side guard `test_immutability_triggers_yield_to_erasure`
    --          (scoped to `plan_purge`) never covered `org_plan_history`.
    --
    --    `app.audit_log` is EXCLUDED from the assertion by name, not because it
    --    sits outside the scopes -- on the production cluster the 264 sweep DID
    --    put the `rgpd_erasure` clause on its append-only guard, so scope (a)
    --    matches there (measured 2026-08-17; the first prod apply of this
    --    migration failed on exactly that) -- but because the clause is inert:
    --    `core.org_purge.PRESERVED_TABLES` (org_purge.py:54) never deletes from
    --    it and it carries no FK cascade from `app.organizations`. The audit
    --    log is the durable trace OF the erasure and must survive it (099);
    --    the conformance suite pins the privilege as deliberately absent.
    -- -----------------------------------------------------------------------
    SELECT string_agg(relname, ', ' ORDER BY relname) INTO offenders
    FROM (
        SELECT DISTINCT c.relname
        FROM pg_class c
        WHERE c.relnamespace = 'app'::regnamespace
          AND c.relkind = 'r'
          AND c.relname <> 'audit_log'  -- preserved by org_purge, see above
          AND NOT has_table_privilege('connector', c.oid, 'DELETE')
          AND (
                EXISTS (
                    SELECT 1 FROM pg_trigger t
                    WHERE t.tgrelid = c.oid
                      AND NOT t.tgisinternal
                      AND (t.tgtype & 8) <> 0
                      AND pg_get_triggerdef(t.oid) ILIKE '%rgpd_erasure%'
                )
             OR EXISTS (
                    SELECT 1 FROM pg_constraint k
                    WHERE k.conrelid = c.oid
                      AND k.contype = 'f'
                      AND k.confrelid = 'app.organizations'::regclass
                      AND k.confdeltype = 'c'
                )
          )
    ) AS still_closed;

    IF offenders IS NOT NULL THEN
        RAISE EXCEPTION
            'AI-67.5: the RGPD erasure hatch is still closed by a missing DELETE '
            'privilege on: %. Each of these either carries a DELETE guard that '
            'yields to `app.rgpd_erasure` or cascades from app.organizations, so '
            'an org erasure will be refused with 42501 before the trigger is '
            'reached. Grant DELETE to `connector` here, in this migration, next '
            'to the two it already names.', offenders;
    END IF;

    RAISE NOTICE
        'AI-67.5: erasure DELETE privilege verified on every hatch-bearing and '
        'cascade-child table in app; UPDATE revoked on the two append-only '
        'ledgers this migration names.';
END
$migration$;

COMMENT ON TABLE app.org_plan_history IS
    'Story 34.1: append-only audit trail of every org_plan change. UPDATE blocked by '
    'trigger AND revoked (056/277). DELETE blocked by the same trigger UNLESS the '
    'transaction flags itself with app.rgpd_erasure (098), and the privilege that lets '
    'a flagged purge reach that trigger is granted here (198/207/277). Both halves of '
    'the posture live in migration 277; earlier files state only one each.';

COMMIT;
