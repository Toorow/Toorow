-- 316 -- a narrow GRANT is DECLARATIVE until the REVOKE lands: eight tables
--        whose stated posture says SELECT, INSERT only, and whose effective
--        posture says UPDATE too.
--
-- THE DEFECT, IN ONE SENTENCE: migration 207 (207:92-99) declared
-- `ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA app GRANT SELECT,
-- INSERT, UPDATE, DELETE ON TABLES TO connector`, and migrations run as the
-- owner, so EVERY table created after 207 reaches `connector` with UPDATE
-- whether or not the creating migration asked for it. A later
-- `GRANT SELECT, INSERT ON app.<table> TO connector` ADDS privileges; it
-- revokes none. The narrow GRANT reads as the posture and is only a comment
-- until a REVOKE states the other half.
--
-- EIGHT TABLES, and for each the declaring migration says SELECT, INSERT
-- (280 adds DELETE -- the erasure path -- and that half is NOT touched here):
--
--   251:765-767  feedback_review_subjects, feedback_eligible_observations,
--                feedback_review_retries
--   252:297-299  feedback_regression_cases, evaluation_assertion_results,
--                feedback_regression_resolutions
--   258:181      mdm_common_key_versions
--   280:105      revoked_browser_sessions
--
-- SEVEN OF THE EIGHT carry an append-only trigger whose WHEN clause yields to
-- `app.rgpd_erasure`, so an UPDATE was refused anyway -- the privilege being
-- open changed the DECLARED posture, not the reachable one. The eighth,
-- `app.revoked_browser_sessions`, carries NO trigger at all: it is the
-- revocation ledger consulted on every authenticated request
-- (`core.session_revocation.is_session_revoked`), and an UPDATE there rewrites
-- the record of who was cut off. That is the case where the privilege itself
-- is the only enforcement, and the one this migration exists for.
--
-- DELETE IS DELIBERATELY UNTOUCHED. On the seven trigger-guarded tables the
-- RGPD erasure hatch needs it (277, and the conformance pin in
-- `server/tests/conformance/test_the_erasure_hatch_is_a_privilege_too.py`);
-- on `revoked_browser_sessions` it is the granted teardown path. Only UPDATE
-- is the half no migration ever stated for these tables.
--
-- THE CONFORMANCE PIN IS GENERALISED with this migration: the file above
-- pinned the absence of UPDATE for `org_plan_history` only; it now pins it
-- for these eight as well, so a future blanket GRANT is caught rather than
-- suffered.
--
-- ERASURE. This migration creates no table and narrows no erasure path; it
-- closes a rewrite path.
--
-- REPLAYABLE. The whole body is wrapped in the `pg_roles` check migrations
-- 049, 056 and 277 use, so it is a no-op on a throwaway base where
-- `connector` does not exist rather than aborting with `undefined_object`.
-- REVOKE of a privilege not held is itself a no-op.

BEGIN;

DO $migration$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'connector') THEN
        RAISE NOTICE
            '316: role `connector` does not exist on this cluster -- nothing to '
            'revoke. The append-only TRIGGERS are the enforcement and are '
            'unaffected.';
        RETURN;
    END IF;

    -- 251: the review evidence is append-only by trigger; the privilege now
    --      says so too.
    EXECUTE 'REVOKE UPDATE ON app.feedback_review_subjects FROM connector';
    EXECUTE 'REVOKE UPDATE ON app.feedback_eligible_observations FROM connector';
    EXECUTE 'REVOKE UPDATE ON app.feedback_review_retries FROM connector';

    -- 252: same posture for the regression evidence.
    EXECUTE 'REVOKE UPDATE ON app.feedback_regression_cases FROM connector';
    EXECUTE 'REVOKE UPDATE ON app.evaluation_assertion_results FROM connector';
    EXECUTE 'REVOKE UPDATE ON app.feedback_regression_resolutions FROM connector';

    -- 258: common-key versions are append-only; the head table keeps UPDATE.
    EXECUTE 'REVOKE UPDATE ON app.mdm_common_key_versions FROM connector';

    -- 280: no trigger rattrape here -- the privilege IS the enforcement.
    EXECUTE 'REVOKE UPDATE ON app.revoked_browser_sessions FROM connector';

    RAISE NOTICE
        '316: UPDATE revoked from `connector` on the eight tables whose narrow '
        'GRANT was declarative only under 207 default privileges; DELETE left '
        'intact everywhere (RGPD hatch / revocation teardown).';
END
$migration$;

COMMIT;
