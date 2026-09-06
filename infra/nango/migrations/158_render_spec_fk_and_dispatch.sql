-- Story 50.3 repair: the Render's Visualization Spec foreign key, a fourth honest
-- literal, and the columns a scheduled dispatch has to record.
--
-- 1. THE FOREIGN KEY MIGRATION 154 CLAIMED AND NEVER CREATED.
--
-- 154 wrapped the composite foreign key from `app.renders` to
-- `app.visualization_spec_versions` in a `DO` block guarded by
-- `to_regclass(...) IS NOT NULL`, with a comment saying it "fires the moment that
-- table exists". It does not. A `DO` block in a migration executes ONCE, when the
-- migration is applied, and at that moment the table did not exist -- so the block
-- evaluated its condition, found NULL, and did nothing, permanently.
--
-- Story 50.4 has since landed the table (migration 156), and the constraint is
-- still absent. Measured on the disposable PostgreSQL before writing this file:
--
--     SELECT conname FROM pg_constraint
--      WHERE conrelid='app.renders'::regclass AND contype='f';
--     -> fk_renders_predecessor, fk_renders_project, fk_renders_result
--
-- Three, not four. Without it, `visualization_spec_version_id` is only checked
-- for SHAPE by `app.is_exact_pin`, so a Render could pin another Project's
-- Visualization Spec version, or one that does not exist at all -- exactly the
-- cross-Project leak every other pin in this table is scoped to prevent.
--
-- This migration adds it unconditionally. `app.visualization_spec_versions` is
-- created by 156 and this is 158, so the dependency is ordered, not hoped for.
-- The lesson is worth stating once: a conditional `DO` block is a decision taken
-- at apply time. It can never be a promise about a later migration.
--
-- 2. A FOURTH EXACT LITERAL FOR A RUN BLOCK, because there is a fourth true case.
--
-- `ck_analysis_notebook_run_blocks_render_is_honest` (154, made NULL-proof by 155)
-- allows a block to carry a Render id, or exactly one of three literals. The
-- service had a branch -- a composition block that pins an accepted presentation,
-- executed while both downstream registries exist -- that set BOTH `render_id` and
-- `render_absent_literal` to NULL, which the constraint refuses. The branch was
-- unreachable only because Story 50.5's registry has not landed; the first
-- Notebook Run after it lands would have raised an opaque CheckViolation and lost
-- the entire Run transaction, every other block included.
--
-- The constraint was right and the code was wrong, so the code is repaired. But
-- the state it was trying to describe is real and has no literal: the block OWES
-- a Render, and a server-side Notebook Run does not mint one. Saying `No Render`
-- there would claim the block was intentionally non-rendered, and saying
-- `No Render: no accepted presentation contract` would claim a contract that
-- exists does not. So a fourth literal is added, and it says the true thing.
--
-- 3. WHAT A DISPATCH RECORDS.
--
-- `app.analysis_notebook_schedules` held `next_due_at` that nothing ever computed
-- and nothing ever read: the panel showed "Enabled: Yes / Next due: --" while no
-- scheduler ever dispatched. `server/core/scheduler.py` now calls the canonical
-- Run service, and a schedule that dispatches has to be able to prove it did.
-- These three columns are operational configuration, not evidence: the Run itself
-- is the evidence, and it is immutable in `app.analysis_notebook_runs`.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. The Render pins a Visualization Spec version of ITS OWN Project.
-- ---------------------------------------------------------------------------
ALTER TABLE app.renders
    DROP CONSTRAINT IF EXISTS fk_renders_visualization_spec_version;

ALTER TABLE app.renders
    ADD CONSTRAINT fk_renders_visualization_spec_version
    FOREIGN KEY (visualization_spec_version_id, org_id, project_id)
    REFERENCES app.visualization_spec_versions (id, org_id, project_id);

COMMENT ON CONSTRAINT fk_renders_visualization_spec_version ON app.renders IS
    'Pin 3 of the ten replay pins, scoped by (id, org_id, project_id) so a Render '
    'cannot pin another Project''s Visualization Spec version. Added by migration '
    '158 because 154 declared it inside a DO block, which ran once -- before '
    'migration 156 created the referenced table -- and therefore never created it.';

-- ---------------------------------------------------------------------------
-- 2. The fourth literal: a Render is owed, and this Run does not mint one.
-- ---------------------------------------------------------------------------
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
                    'No Render: no accepted presentation contract',
                    'No Render: rendering is not dispatched by a Notebook Run'
                )
            ),
            FALSE
        )
    );

-- ---------------------------------------------------------------------------
-- 3. What a dispatch records. Operational, mutable, and it holds no content.
-- ---------------------------------------------------------------------------
ALTER TABLE app.analysis_notebook_schedules
    ADD COLUMN IF NOT EXISTS last_dispatched_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_run_id        TEXT,
    ADD COLUMN IF NOT EXISTS last_dispatch_note TEXT;

ALTER TABLE app.analysis_notebook_schedules
    DROP CONSTRAINT IF EXISTS fk_analysis_notebook_schedules_last_run;

-- The Run this schedule last produced is named by identity, never by recency.
-- A scoped composite key, so a schedule cannot point at another Project's Run.
ALTER TABLE app.analysis_notebook_schedules
    ADD CONSTRAINT fk_analysis_notebook_schedules_last_run
    FOREIGN KEY (last_run_id, org_id, project_id)
    REFERENCES app.analysis_notebook_runs (id, org_id, project_id);

ALTER TABLE app.analysis_notebook_schedules
    DROP CONSTRAINT IF EXISTS ck_analysis_notebook_schedules_dispatch_is_paired;

-- Either a dispatch happened and both facts are recorded, or none did. A
-- timestamp without the Run it produced is the "it ran, trust me" state.
ALTER TABLE app.analysis_notebook_schedules
    ADD CONSTRAINT ck_analysis_notebook_schedules_dispatch_is_paired CHECK (
        COALESCE(
            (last_run_id IS NULL AND last_dispatched_at IS NULL)
            OR (last_run_id IS NOT NULL AND last_dispatched_at IS NOT NULL),
            FALSE
        )
    );

COMMIT;
