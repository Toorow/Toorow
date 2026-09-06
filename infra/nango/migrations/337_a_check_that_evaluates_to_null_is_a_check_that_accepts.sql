-- 337: two corrections that a frozen migration cannot make about itself.
--
-- PART 1 -- THIRTEEN CHECKS THAT ACCEPT WHAT THEY NAME. A CHECK rejects FALSE
-- only: an expression that evaluates to NULL is ACCEPTED. Migration 155 closed
-- this on Story 50.3's four constraints, 159 swept the whole schema and closed
-- five more, and `server/tests/core/test_check_constraints_null_proof_pg.py`
-- was written the same day to keep the class shut.
--
-- IT HAS NEVER BEEN GREEN ON A COMPLETE SET. Measured 2026-09-01 on a
-- disposable cluster carrying all 336 migrations (port 55432, 193 s):
--
--     python -m pytest server/tests/core/test_check_constraints_null_proof_pg.py
--       -> 13 CHECK constraint(s) evaluate to NULL on a legal row
--
-- Thirteen, spread from migration 162 to migration 333. The guard landed 1 h 26
-- after the first of them, which is why a regression test that was never run to
-- completion on a full schema proves nothing: it was red the day it was written
-- and stayed red under every partial run since.
--
-- THE FORM, AND THE ONE PLACE IT IS NOT `COALESCE(..., FALSE)`. Twelve of the
-- thirteen are multi-branch contracts whose branches all compare a NULLABLE
-- column: make every branch NULL and the row satisfies none of them and is
-- taken anyway. Those are wrapped in `COALESCE(<expr>, FALSE)`, 155's form,
-- unchanged and under the SAME constraint name.
--
-- `ck_render_shares_bearer_hash_shape` is the exception and wrapping it would be
-- a destructive change dressed as a repair. Migration 162 wrote `bearer_hash
-- TEXT NOT NULL` with `CHECK (bearer_hash ~ '^[0-9a-f]{64}$')`; migration 323
-- DROPPED that NOT NULL on purpose -- a share in `pending_confirmation` has no
-- bearer yet -- and 323's own `ck_render_shares_pending_has_no_bearer` requires
-- the NULL there while `ck_render_shares_active_has_a_bearer` requires the hash
-- once the share is active. So NULL is a legal, mandatory state here, and the
-- honest null-proof form STATES it: `bearer_hash IS NULL OR <shape>`. That
-- expression is never NULL, and it accepts strictly more than the form it
-- replaces, so it can reject no existing row. 159's header warns about exactly
-- this and the warning is followed rather than quoted.
--
-- IT MEASURES BEFORE IT TIGHTENS, AND IT NEVER REWRITES DATA. A tightened CHECK
-- fails on any existing row that violates it, and this file is applied to a
-- production database whose rows this session has not seen. The `DO` block below
-- counts, per constraint, the rows whose CURRENT expression evaluates to NULL --
-- exactly the rows the `COALESCE` form would newly reject, since a row that made
-- the old expression TRUE still makes the new one TRUE -- and RAISES with the
-- table and the count. The operator then decides what those rows mean. An
-- `UPDATE` that silently invented values for them would destroy the evidence of
-- the very state these constraints exist to make impossible.
--
-- The probe reads `pg_constraint` rather than repeating the thirteen expressions
-- as strings: a second copy of an expression is a second authority, and it would
-- drift from the `ALTER` twenty lines below it the first time one of them was
-- edited. It also asserts that it found all thirteen, because `DROP CONSTRAINT
-- IF EXISTS` on a name that no longer exists is silent, and a silent probe over
-- nothing is the shape of green that means nothing.
--
-- MEASURED ON THE FIXTURE (disposable cluster, 336 migrations, 2026-09-01):
-- ZERO NULL-accepted rows on all twelve probed constraints. That zero is not a
-- zero over empty tables -- the cluster carries the rows the local suites have
-- written, and the probe ran over them:
--
--     visualization_template_versions   6102     render_shares            147
--     nightly_step_runs                 1454     feedback_annotations     118
--     feedback_eligible_observations     162     render_share_feedback     27
--     inbound_raw_imports                  6     the other four             0
--
-- That is still a property of THAT database and not a prediction about
-- production; the `DO` block is what makes production's own number appear,
-- named, instead of a mute constraint violation.
--
-- PART 2 -- THE CLAIM MIGRATION 336 CANNOT WITHDRAW. 336 created
-- `app.semantic_recompile_attempts` and wrote:
--
--     "Both foreign keys cascade, so the org eraser (`core.org_purge.plan_purge`,
--      which walks the foreign keys) removes these rows with the version."
--
-- The two halves of that sentence contradict each other.
-- `org_purge._FK_GRAPH_SQL` filters `confdeltype IN ('a','r')` -- NO ACTION and
-- RESTRICT ONLY -- because a CASCADE child is resolved by Postgres itself. A
-- table whose every foreign key is ON DELETE CASCADE is therefore erased, and
-- `org_purge` names it in ZERO of its statements. Measured before this file was
-- written: `plan_purge(conn, 'org_EXAMPLE')` emits 3391 statements over 203
-- tables and `semantic_recompile_attempts` is in none of them.
--
-- WHY THE CORRECTION IS THIS ONE AND NOT THE DENIAL. 336 is applied;
-- `apply_migrations.py` raises `checksum drift: applied migration changed` on any
-- edit to it, so the canonical phrase `NOT `core.org_purge`` cannot be added to
-- its header. `test_migration_erasure_claims.FROZEN_CLAIMS` is the inventory for
-- exactly that predicament and it refuses this file by name --
-- `assert max(int(name[:3]) for name in FROZEN_CLAIMS) < 200`. The guard states
-- two ways out and only two; one of them is closed by the checksum and the other
-- is this: give the table a foreign key the graph can see. So 336's sentence is
-- made TRUE rather than annotated as false.
--
-- `project_id` becomes ON DELETE RESTRICT -- the shape 57 of the 124 foreign keys
-- pointing at `app.projects` already carry, and the shape `org_purge` was built
-- around ("deleting a project must not silently erase its datastreams",
-- `core/org_purge.py`). `view_version_id` STAYS ON DELETE CASCADE: an attempt is
-- subordinate to the version it was made against and must die with it, which is
-- the half of 336's sentence that was already true. The only delete path this
-- changes is `DELETE FROM app.projects` -- one production caller
-- (`core/projects_api.py:464`, the rollback of a project whose tenant key failed
-- to provision, on a project that cannot yet carry an attempt) -- and the RGPD
-- erasure, which now emits a statement that NAMES the table.
--
-- Not applied to production by the session that wrote it.

BEGIN;

-- ---------------------------------------------------------------------------
-- 0. THE MEASUREMENT. Nothing is tightened until this block has said zero.
-- ---------------------------------------------------------------------------

DO $probe$
DECLARE
    -- The thirteen, by name. `ck_render_shares_bearer_hash_shape` is in the list
    -- so its existence is asserted with the others; it is skipped in the count
    -- because its new form accepts strictly more than the one it replaces.
    targets  CONSTANT TEXT[] := ARRAY[
        'ck_evaluation_case_verdicts_producer',
        'ck_feedback_annotations_eligibility_schema',
        'ck_feedback_annotations_target',
        'ck_feedback_eligible_observations_documents',
        'ck_feedback_regression_cases_path',
        'ck_inbraw_dispatch_bundle_pair',
        'managed_file_dispatches_dq_evidence_check',
        'mdm_metric_dimension_versions_head_check',
        'ck_nightly_step_runs_ends_after_it_starts',
        'ck_render_share_feedback_eligibility_schema',
        'ck_render_share_feedback_exact_target',
        'ck_render_shares_bearer_hash_shape',
        'ck_visualization_template_versions_document_pins'
    ];
    found     TEXT[] := ARRAY[]::TEXT[];
    row_probe RECORD;
    offending BIGINT;
    report    TEXT := '';
BEGIN
    FOR row_probe IN
        SELECT c.conrelid::regclass::text          AS tbl,
               c.conname                           AS con,
               pg_get_expr(c.conbin, c.conrelid)   AS expr
          FROM pg_constraint c
         WHERE c.contype = 'c'
           AND c.connamespace = 'app'::regnamespace
           AND c.conname = ANY (targets)
         ORDER BY 1, 2
    LOOP
        found := found || row_probe.con;
        CONTINUE WHEN row_probe.con = 'ck_render_shares_bearer_hash_shape';
        -- A row the tightened form newly rejects is a row whose CURRENT
        -- expression is NULL: it is accepted today for that reason and no other.
        EXECUTE format('SELECT count(*) FROM %s WHERE (%s) IS NULL',
                       row_probe.tbl, row_probe.expr)
           INTO offending;
        IF offending > 0 THEN
            report := report || format(E'\n    %s (%s): %s row(s)',
                                       row_probe.tbl, row_probe.con, offending);
        END IF;
    END LOOP;

    IF array_length(found, 1) IS DISTINCT FROM array_length(targets, 1) THEN
        RAISE EXCEPTION
            'migration 337 found % of the % constraints it repairs; missing: %',
            COALESCE(array_length(found, 1), 0),
            array_length(targets, 1),
            (SELECT array_agg(t) FROM unnest(targets) AS t WHERE t <> ALL (found))
        USING HINT =
            'a constraint was renamed or dropped after 2026-09-01. Re-run '
            'server/tests/core/test_check_constraints_null_proof_pg.py against '
            'this database and repair the names this file carries.';
    END IF;

    IF report <> '' THEN
        RAISE EXCEPTION
            'migration 337 refuses to tighten: rows already exist that the '
            'null-proof form rejects, per table and constraint: %', report
        USING HINT =
            'each of those rows satisfies NO branch of its constraint and was '
            'taken because the expression evaluated to NULL. Decide what they '
            'mean before this migration runs -- correct them through the '
            'product write that owns the table, or delete them. This migration '
            'will not rewrite them: an UPDATE here would invent the value the '
            'row never carried and erase the evidence of the state these '
            'constraints exist to make impossible.';
    END IF;

    RAISE NOTICE 'migration 337: % constraints located, 0 NULL-accepted rows',
                 array_length(found, 1);
END
$probe$;

-- ---------------------------------------------------------------------------
-- 1. The twelve multi-branch contracts: 155's form, same names.
-- ---------------------------------------------------------------------------

-- 252. A verdict declared by the evaluator NAMES the contract hash it ran under;
-- one declared by the caller carries none. The evaluator with a NULL hash was the
-- third, unnamed state.
ALTER TABLE app.evaluation_case_dimension_verdicts
    DROP CONSTRAINT IF EXISTS ck_evaluation_case_verdicts_producer;
ALTER TABLE app.evaluation_case_dimension_verdicts
    ADD CONSTRAINT ck_evaluation_case_verdicts_producer CHECK (
        COALESCE(
            (producer = 'caller-declared.v1' AND producer_contract_hash IS NULL)
            OR
            (producer = 'result-case-evaluator.v1'
             AND producer_contract_hash =
                 'd8e8a0a4ccdcb2e61c25c2206a60d15ad7da306ab229c83ad1a76eaeb74b4add'),
            FALSE)
    );

-- 251. An eligibility version is stated only about an exact-feedback target. A
-- stated eligibility with NO target schema version satisfied neither branch.
ALTER TABLE app.feedback_annotations
    DROP CONSTRAINT IF EXISTS ck_feedback_annotations_eligibility_schema;
ALTER TABLE app.feedback_annotations
    ADD CONSTRAINT ck_feedback_annotations_eligibility_schema CHECK (
        COALESCE(
            eligibility_schema_version IS NULL
            OR (
                target_schema_version = 'exact-feedback.v1'
                AND eligibility_schema_version = 'feedback-eligibility.v1'
            ),
            FALSE)
    );

-- 249. An exact annotation points at an answer, a datum or a path step. A stated
-- schema version with `target_kind` NULL pointed at nothing and was taken.
ALTER TABLE app.feedback_annotations
    DROP CONSTRAINT IF EXISTS ck_feedback_annotations_target;
ALTER TABLE app.feedback_annotations
    ADD CONSTRAINT ck_feedback_annotations_target CHECK (
        COALESCE(
            target_schema_version IS NULL
            OR (
                target_kind = 'answer'
                AND datum_row_index IS NULL AND datum_field IS NULL AND path_step_ordinal IS NULL
            )
            OR (
                target_kind = 'datum'
                AND datum_row_index >= 0 AND datum_field IS NOT NULL
                AND length(btrim(datum_field)) BETWEEN 1 AND 200
                AND path_step_ordinal IS NULL
            )
            OR (
                target_kind = 'path_step'
                AND datum_row_index IS NULL AND datum_field IS NULL
                AND path_step_ordinal IS NOT NULL AND ai_path_id IS NOT NULL
            ),
            FALSE)
    );

-- 251. The three documents of an eligible observation declare their versions. An
-- object missing the key compared NULL to the literal, and the AND chain went
-- NULL rather than FALSE.
ALTER TABLE app.feedback_eligible_observations
    DROP CONSTRAINT IF EXISTS ck_feedback_eligible_observations_documents;
ALTER TABLE app.feedback_eligible_observations
    ADD CONSTRAINT ck_feedback_eligible_observations_documents CHECK (
        COALESCE(
            jsonb_typeof(authority) = 'object'
            AND classification->>'schema_version' = 'evaluation-classification.v1'
            AND classification->>'classification_hash' = classification_hash
            AND compatibility_preimage->>'schema_version' = 'feedback-compatibility.v1',
            FALSE)
    );

-- 252. A regression case names its AI Path, or says in exact words that there is
-- none. Both NULL was the third state.
ALTER TABLE app.feedback_regression_cases
    DROP CONSTRAINT IF EXISTS ck_feedback_regression_cases_path;
ALTER TABLE app.feedback_regression_cases
    ADD CONSTRAINT ck_feedback_regression_cases_path CHECK (
        COALESCE(
            (ai_path_id IS NOT NULL AND ai_path_absent_literal IS NULL)
            OR (ai_path_id IS NULL AND ai_path_absent_literal = 'No AI path'),
            FALSE)
    );

-- 194. A dispatch bundle and its fingerprint arrive together or not at all. A
-- bundle with a NULL fingerprint satisfied neither branch.
ALTER TABLE app.inbound_raw_imports
    DROP CONSTRAINT IF EXISTS ck_inbraw_dispatch_bundle_pair;
ALTER TABLE app.inbound_raw_imports
    ADD CONSTRAINT ck_inbraw_dispatch_bundle_pair CHECK (
        COALESCE(
            (dispatch_bundle IS NULL AND dispatch_bundle_fingerprint IS NULL)
            OR (jsonb_typeof(dispatch_bundle) = 'object'
                AND dispatch_bundle_fingerprint ~ '^[0-9a-f]{64}$'),
            FALSE)
    );

-- 194. Data-quality evidence is absent, or it says `passed`. An object with no
-- `status` key was neither.
ALTER TABLE app.managed_file_dispatches
    DROP CONSTRAINT IF EXISTS managed_file_dispatches_dq_evidence_check;
ALTER TABLE app.managed_file_dispatches
    ADD CONSTRAINT managed_file_dispatches_dq_evidence_check CHECK (
        COALESCE(
            dq_evidence IS NULL
            OR (jsonb_typeof(dq_evidence) = 'object'
                AND dq_evidence->>'status' = 'passed'),
            FALSE)
    );

-- 315. The head of a grain names a canonical field. An object with no
-- `canonical_field_id` key made the regex NULL and the row was taken -- a version
-- of a metric grain whose head names nothing, in an append-only table.
ALTER TABLE app.mdm_metric_dimension_versions
    DROP CONSTRAINT IF EXISTS mdm_metric_dimension_versions_head_check;
ALTER TABLE app.mdm_metric_dimension_versions
    ADD CONSTRAINT mdm_metric_dimension_versions_head_check CHECK (
        COALESCE(
            jsonb_typeof(head) = 'object'
            AND head->>'canonical_field_id' ~ '^mdm_[0-9A-HJKMNP-TV-Z]{26}$',
            FALSE)
    );

-- 325. A step that ended, ended after it started. `ended_at` set with
-- `started_at` NULL made the comparison NULL. Its companion
-- `ck_nightly_step_runs_ended_implies_started` already refuses that row, so this
-- form rejects nothing the table did not already refuse -- but a constraint is
-- read one at a time, and this one said nothing.
ALTER TABLE app.nightly_step_runs
    DROP CONSTRAINT IF EXISTS ck_nightly_step_runs_ends_after_it_starts;
ALTER TABLE app.nightly_step_runs
    ADD CONSTRAINT ck_nightly_step_runs_ends_after_it_starts CHECK (
        COALESCE(ended_at IS NULL OR ended_at >= started_at, FALSE)
    );

-- 251. The same contract as `feedback_annotations`, on the shared surface.
ALTER TABLE app.render_share_feedback
    DROP CONSTRAINT IF EXISTS ck_render_share_feedback_eligibility_schema;
ALTER TABLE app.render_share_feedback
    ADD CONSTRAINT ck_render_share_feedback_eligibility_schema CHECK (
        COALESCE(
            eligibility_schema_version IS NULL
            OR (
                target_schema_version = 'exact-feedback.v1'
                AND eligibility_schema_version = 'feedback-eligibility.v1'
            ),
            FALSE)
    );

-- 249. The same contract as `ck_feedback_annotations_target`, on the shared
-- surface, with the extra `path_step_ordinal >= 0` that surface carries.
ALTER TABLE app.render_share_feedback
    DROP CONSTRAINT IF EXISTS ck_render_share_feedback_exact_target;
ALTER TABLE app.render_share_feedback
    ADD CONSTRAINT ck_render_share_feedback_exact_target CHECK (
        COALESCE(
            target_schema_version IS NULL
            OR (target_kind = 'answer' AND datum_row_index IS NULL
                AND datum_field IS NULL AND path_step_ordinal IS NULL)
            OR (target_kind = 'datum' AND datum_row_index >= 0 AND datum_field IS NOT NULL
                AND length(btrim(datum_field)) BETWEEN 1 AND 200
                AND path_step_ordinal IS NULL)
            OR (target_kind = 'path_step' AND datum_row_index IS NULL
                AND datum_field IS NULL AND path_step_ordinal IS NOT NULL
                AND path_step_ordinal >= 0 AND ai_path_id IS NOT NULL),
            FALSE)
    );

-- 333. A template document restates its own identity and may not disagree with
-- the columns. A document missing a key compared NULL to the column and the AND
-- chain went NULL -- a template version pinning nothing, in an append-only table.
ALTER TABLE app.visualization_template_versions
    DROP CONSTRAINT IF EXISTS ck_visualization_template_versions_document_pins;
ALTER TABLE app.visualization_template_versions
    ADD CONSTRAINT ck_visualization_template_versions_document_pins CHECK (
        COALESCE(
            document->>'spec_contract_version' = spec_contract_version
            AND (document->'schema_version') = to_jsonb(schema_version)
            AND document->>'family' = family,
            FALSE)
    );

-- ---------------------------------------------------------------------------
-- 2. The thirteenth: NULL is a legal state here, so the constraint says so.
-- ---------------------------------------------------------------------------

-- 162 wrote the shape while `bearer_hash` was NOT NULL; 323 dropped the NOT NULL
-- for `pending_confirmation` and left the shape reading NULL for those rows.
-- Stating the legal NULL is the null-proof form; `COALESCE(..., FALSE)` here
-- would reject every share that has not yet been confirmed.
ALTER TABLE app.render_shares
    DROP CONSTRAINT IF EXISTS ck_render_shares_bearer_hash_shape;
ALTER TABLE app.render_shares
    ADD CONSTRAINT ck_render_shares_bearer_hash_shape CHECK (
        bearer_hash IS NULL OR bearer_hash ~ '^[0-9a-f]{64}$'
    );

-- ---------------------------------------------------------------------------
-- 3. Migration 336's sentence about `core.org_purge` becomes true.
-- ---------------------------------------------------------------------------

ALTER TABLE app.semantic_recompile_attempts
    DROP CONSTRAINT IF EXISTS semantic_recompile_attempts_project_id_fkey;
ALTER TABLE app.semantic_recompile_attempts
    ADD CONSTRAINT semantic_recompile_attempts_project_id_fkey
        FOREIGN KEY (project_id) REFERENCES app.projects (id) ON DELETE RESTRICT;

COMMENT ON CONSTRAINT semantic_recompile_attempts_project_id_fkey
    ON app.semantic_recompile_attempts IS
    'AI-346/337: RESTRICT, not CASCADE, and that is the whole point. '
    '`core.org_purge.plan_purge` walks `confdeltype IN (''a'',''r'')` only, so a '
    'CASCADE-only table is erased by Postgres and named in none of the eraser''s '
    'own statements. Migration 336 claimed the eraser reached these rows; this '
    'edge is what makes that claim true. `view_version_id` stays ON DELETE '
    'CASCADE -- an attempt dies with the version it was made against.';

COMMIT;
