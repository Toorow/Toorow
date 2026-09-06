-- 207 -- the least-privilege role existed, owned nothing and could read nothing
--
-- MEASURED IN PRODUCTION, 2026-08-04, before writing a line:
--
--   owner of app.projects .................... postgres
--   role `connector` ......................... exists, NOLOGIN
--   app/toorow_meta base tables .............. 287
--   of which `connector` can SELECT .......... 0
--   role the application actually connects as  postgres (rolbypassrls = true)
--
-- So the application runs in production as a BYPASSRLS role, and `connector` --
-- the role every migration since 002 writes REVOKE statements against, the role
-- `scripts/disposable_postgres.py` runs the whole pg-gated suite as -- holds no
-- privilege on anything. Two consequences, and the second is the reason this
-- migration exists rather than a ticket:
--
--   1. every RLS policy in production is vacuous, because BYPASSRLS skips them
--      all. That is AI-100, and this migration does NOT fix it: switching the
--      application's DSN is a deployment act, not a schema change.
--   2. the local recipe cannot model production. It makes `connector` the OWNER
--      to get a working database, and a REVOKE against an owner materialises an
--      EMPTY acl -- which is how migration 199 came to fail locally and pass in
--      production. Trying `OWNER postgres` locally on 2026-08-04 applied 206/206
--      migrations and then left 104 of 289 tables unreadable. Not an oversight
--      to patch: NO migration emits ALTER DEFAULT PRIVILEGES and only four carry
--      a GRANT to `connector`. The schema has always leaned on ownership.
--
-- WHAT THIS CHANGES FOR THE RUNNING APPLICATION: nothing. `connector` is NOLOGIN
-- and nothing connects as it. Granting rights to a role no session assumes cannot
-- alter a single request. That is the property that makes this safe to apply.
-- What it buys is that the least-privilege posture becomes REACHABLE and, for the
-- first time, TESTABLE -- the local suite already runs as this role.
--
-- WHAT THE BLANKET GRANT REOPENS, AND WHY THAT IS THE RIGHT LINE. It hands
-- UPDATE and DELETE back on the ~128 `app` tables carrying an immutability
-- trigger. The first draft of this migration revoked them all again, computed
-- from the trigger definitions -- and that was WRONG, measured:
--
--   test_22_activation_is_derived_and_fails_closed_when_nothing_is_published
--   -> InsufficientPrivilege: permission denied for governance_rule_set_versions
--
-- because `trg_governance_rule_set_versions_immutable` is not a no-UPDATE rule.
-- It rejects mutations SELECTIVELY, and `governance_rule_sets.py:615` legitimately
-- runs `UPDATE ... SET status = 'published'` through it. "Has an immutability
-- trigger" and "must hold no UPDATE privilege" are different statements, and a
-- regex over trigger names cannot tell them apart. So the revokes below are the
-- ones MIGRATIONS DECLARED, verbatim -- the trigger is the enforcement, and every
-- migration that wrote one says so itself ("REVOKE is best-effort (the trigger
-- enforces immutability regardless)").
--
-- Nor is this a loosening: before this migration `connector` held NOTHING on any
-- of them, so there is no working state being widened. The role the application
-- actually uses today holds all of it, plus BYPASSRLS.
--
-- DOCTRINE FOR THE NEXT APPEND-ONLY TABLE, same shape as the RGPD hatch: if it
-- wants the privilege closed as well as the trigger, its own migration must say
-- so. The default privileges set below grant DML on every future table.

BEGIN;

-- 1. Reach the schemas at all.
GRANT USAGE ON SCHEMA app TO connector;
GRANT USAGE ON SCHEMA toorow_meta TO connector;

-- 1b. CREATE, and only because it was MEASURED to be needed -- running the
--     pg-gated suites as a non-owner for the first time named these two exactly:
--
--       InsufficientPrivilege: permission denied for database toorow_test
--       InsufficientPrivilege: permission denied for schema app
--
--     The first is the per-org data plane of epic 24: `warehouse_tenancy` creates
--     `org_<slug>_raw` / `_marts` schemas at provisioning time, and `org_purge`
--     drops them. An owner never noticed needing the privilege. The second is the
--     same story one level down. Nothing here is speculative: no other object was
--     refused across `tests/integration` + `tests/core/test_fee_tax_rules.py`.
DO $$
BEGIN
    EXECUTE format('GRANT CREATE ON DATABASE %I TO connector', current_database());
END
$$;
GRANT CREATE ON SCHEMA app TO connector;

-- 2. Existing objects.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA app TO connector;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA toorow_meta TO connector;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA app TO connector;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA toorow_meta TO connector;

-- 3. Future objects. Migrations run as the owner, so the default privileges are
--    declared FOR THAT ROLE -- omitting `FOR ROLE` would tie them to whoever
--    happens to run this statement and silently miss every later migration.
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA app
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO connector;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA toorow_meta
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO connector;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA app
  GRANT USAGE, SELECT ON SEQUENCES TO connector;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA toorow_meta
  GRANT USAGE, SELECT ON SEQUENCES TO connector;

-- 4. Re-state, verbatim, the narrower postures earlier migrations DECLARED.
--    They are restated rather than assumed because step 2 ran after them.

--    002: the audit log is append-only for the application role.
REVOKE UPDATE, DELETE ON app.audit_log FROM connector;
REVOKE UPDATE, DELETE ON app.metric_semantics_audit FROM connector;

--    183: this ledger counts operations in order to REFUSE the next one, so the
--    application role must not reach it at all; it is written through a SECURITY
--    DEFINER function. 199 then reopened exactly what RGPD erasure needs, and no
--    more -- DELETE, plus SELECT on two columns.
REVOKE ALL ON TABLE app.datastream_inbound_credential_rate_events FROM PUBLIC, connector;
GRANT DELETE ON TABLE app.datastream_inbound_credential_rate_events TO connector;
GRANT SELECT (datastream_id, operation_id)
  ON TABLE app.datastream_inbound_credential_rate_events TO connector;

--    198: org plan history is append-only, and erasure must still reach it.
GRANT DELETE ON app.org_plan_history TO connector;

--    040.3: inbound brand-match provenance is immutable for everyone.
REVOKE UPDATE, DELETE ON app.inbound_brand_match_decisions FROM PUBLIC, connector;

COMMIT;
