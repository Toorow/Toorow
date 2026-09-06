-- Story 50.3, correction to migration 154: three CHECKs were satisfiable by NULL.
--
-- WHY THIS IS A SECOND MIGRATION AND NOT AN EDIT. 154 was applied before the
-- defect was measured, and this repository's rule is absolute: an applied
-- migration is never re-edited, it is corrected by the next one. Re-editing would
-- make the ledger checksum disagree with the file on any database that had
-- already run it.
--
-- THE DEFECT, and it is the reason all three "honest literal" CHECKs existed.
-- A CHECK constraint is satisfied when its expression evaluates to NULL, not only
-- when it is TRUE. So:
--
--     (kind IN (...) AND ... AND absent IS NULL)          -- NULL when kind IS NULL
--     OR (kind IS NULL AND ... AND absent = 'literal')    -- NULL when absent IS NULL
--
-- with every column NULL evaluates to `NULL OR NULL` = NULL, and the row is
-- ACCEPTED. That is precisely the "fill it in later" state the pattern was written
-- to make impossible: a Report version with no presentation reference and no
-- literal, a Notebook Run block with no Render and no reason, and a terminal
-- Notebook Run with no outcome, all three indistinguishable from a complete row
-- until someone reads them.
--
-- Measured by `server/tests/core/test_analyze_artifacts_pg.py`, whose `None`
-- parameter cases failed against 154 and pass against this. The three tests were
-- written before the fix and are what found it.
--
-- THE FIX. Wrap each disjunction in COALESCE so the expression is never NULL.
-- Nothing else changes: a row that satisfied 154 legitimately still satisfies this.

BEGIN;

-- 1. A Report version states its presentation, or the exact literal. Never neither.
ALTER TABLE app.analysis_report_versions
    DROP CONSTRAINT IF EXISTS ck_analysis_report_versions_presentation_is_honest;
ALTER TABLE app.analysis_report_versions
    ADD CONSTRAINT ck_analysis_report_versions_presentation_is_honest CHECK (
        COALESCE(
            (
                presentation_kind
                    IN ('visualization_template_version', 'visualization_spec_version')
                AND app.is_exact_pin(presentation_version_id)
                AND presentation_absent_literal IS NULL
            )
            OR (
                presentation_kind IS NULL
                AND presentation_version_id IS NULL
                AND presentation_absent_literal = 'No accepted presentation contract'
            ),
            FALSE
        )
    );

-- 2. Same contract on a composition block.
ALTER TABLE app.analysis_notebook_version_blocks
    DROP CONSTRAINT IF EXISTS ck_analysis_notebook_blocks_presentation_is_honest;
ALTER TABLE app.analysis_notebook_version_blocks
    ADD CONSTRAINT ck_analysis_notebook_blocks_presentation_is_honest CHECK (
        COALESCE(
            (
                presentation_kind
                    IN ('visualization_template_version', 'visualization_spec_version')
                AND app.is_exact_pin(presentation_version_id)
                AND presentation_absent_literal IS NULL
            )
            OR (
                presentation_kind IS NULL
                AND presentation_version_id IS NULL
                AND presentation_absent_literal = 'No accepted presentation contract'
            ),
            FALSE
        )
    );

-- 3. A Run block names a Render, or says in exact words why there is none.
ALTER TABLE app.analysis_notebook_run_blocks
    DROP CONSTRAINT IF EXISTS ck_analysis_notebook_run_blocks_render_is_honest;
ALTER TABLE app.analysis_notebook_run_blocks
    ADD CONSTRAINT ck_analysis_notebook_run_blocks_render_is_honest CHECK (
        COALESCE(
            (render_id IS NOT NULL AND render_absent_literal IS NULL)
            OR (
                render_id IS NULL
                AND render_absent_literal IN (
                    'No Render',
                    'No Render: block failed',
                    'No Render: no accepted presentation contract'
                )
            ),
            FALSE
        )
    );

-- 4. A terminal Notebook Run states its outcome. `state='terminal'` with a NULL
--    outcome passed 154 for the same reason.
ALTER TABLE app.analysis_notebook_runs
    DROP CONSTRAINT IF EXISTS ck_analysis_notebook_runs_terminal_is_complete;
ALTER TABLE app.analysis_notebook_runs
    ADD CONSTRAINT ck_analysis_notebook_runs_terminal_is_complete CHECK (
        COALESCE(
            (
                state = 'terminal' AND terminal_at IS NOT NULL
                AND outcome IN ('succeeded', 'partial', 'failed')
            )
            OR (state <> 'terminal' AND terminal_at IS NULL AND outcome IS NULL),
            FALSE
        )
    );

COMMIT;
