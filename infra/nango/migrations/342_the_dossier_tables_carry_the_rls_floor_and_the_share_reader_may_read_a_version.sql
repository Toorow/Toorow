-- 342 -- the two Dossier tables carry the Epic-36 RLS floor, and the public
--        share reader may read the ONE thing the dossier page needs.
--
-- TWO DEFECTS OF MIGRATIONS 340/341, MEASURED 2026-09-04 ON A CLUSTER REBUILT
-- FROM SCRATCH (341 migrations), before the deployment window:
--
--   1. `tests/core/test_rls_covers_every_org_scoped_table_pg.py` -- the AI-299
--      ratchet -- names `analysis_dossiers` and `analysis_dossier_versions` as
--      the only two tables of `app` that carry `org_id` and NO row-level policy
--      (`pg_class.relrowsecurity = false` on both). Migration 154 armed every
--      other analysis object (reports, notebooks, renders) with the `_strict`
--      policy; 340 created its two tables and forgot the floor. The application
--      check still runs first ("RLS is the floor, not the door", 154 AC13), so
--      nothing leaked that we know of -- but a floor that does not cover a table
--      cannot catch the seam that forgets the check, which is what the ratchet
--      exists to say.
--
--   2. `SET LOCAL ROLE toorow_share_reader; SELECT label, version_number, blocks
--      FROM app.analysis_dossier_versions` -> SQLSTATE 42501. 341 taught a Share
--      to open a Dossier version and `render_shares.dossier_sequence` reads that
--      row under the reader role -- but 341 granted the role nothing, so every
--      dossier share page answers the mute 401 of `_session_dossier` in
--      production today. The pg test of that door monkeypatches
--      `dossier_sequence`, which is how a route without a grant stayed green.
--
-- WHAT THIS DOES, AND NO MORE. The same `_strict` policy as 154, verbatim, on
-- both tables (a policy that reads `toorow.enforce_epic36` is inert for the
-- reader role, which never arms it -- exactly as `app.renders` behaves on the
-- same page). And SELECT on `app.analysis_dossier_versions` for
-- `toorow_share_reader`: the head table stays unreadable to it, `app.projects`,
-- `app.query_results` and the Result payloads stay unreadable to it, so the
-- page still cannot reach Project data. Nothing is dropped, nothing is edited.

DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'analysis_dossiers',
        'analysis_dossier_versions'
    ] LOOP
        EXECUTE format('ALTER TABLE app.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE app.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON app.%I', t || '_strict', t);
        EXECUTE format(
            'CREATE POLICY %I ON app.%I '
            'USING (current_setting(''toorow.enforce_epic36'', true) IS DISTINCT FROM ''on'' '
            '       OR app.epic36_has_resource_access(org_id, ''project'', project_id)) '
            'WITH CHECK (current_setting(''toorow.enforce_epic36'', true) IS DISTINCT FROM ''on'' '
            '       OR app.epic36_has_resource_access(org_id, ''project'', project_id))',
            t || '_strict', t
        );
    END LOOP;
END $$;

-- The reader role is a cluster prerequisite (162 refuses to run without it),
-- so it exists wherever this migration runs.
GRANT SELECT ON app.analysis_dossier_versions TO toorow_share_reader;
