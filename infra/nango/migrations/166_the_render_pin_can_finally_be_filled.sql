-- Epic 51: the rendered-artifact pin becomes fillable, because its owner delivered.
--
-- WHAT CHANGED OUTSIDE THIS EPIC. Migration 153 declared five render pins and held
-- every one of them NULL, for a reason it stated plainly in its header: "the
-- rendered-artifact object of Stories 50.4 / 50.5 / 50.7 does not exist -- the
-- rendering stack is not even installed". That was true on 2026-07-31 morning.
--
-- It is no longer. Measured before writing a line of this file:
--
--     50.4 Visualization Spec and builder      -> review, delivered
--     50.5 shared renderer registry + runtime  -> review, delivered
--     app.renders                UNIQUE (id, org_id, project_id)
--     app.renderer_runtime_builds  PRIMARY KEY (id), platform-scoped
--
-- So every Epic 51 verdict still reporting `unverifiable / render_owner_not_delivered`
-- is now saying something FALSE: the owner delivered. An honest absence that outlives
-- its cause becomes a lie with good manners, and it is worse than a gap because it
-- looks maintained.
--
-- WHAT THIS MIGRATION DELIBERATELY DOES NOT LIFT, and why each stays:
--
--   1. `golden_question_versions.expected_render_ref` STAYS HELD NULL. Not because
--      the object is missing -- it exists now -- but because the ratified Golden
--      Question contract (`analyze-and-test.md:249-259`) lists SEVEN required
--      fields and an expected rendered artifact is not among them. A Golden
--      Question is a specification; a Render is produced by a run. Pinning a
--      concrete instance in a specification would be inventing a product decision
--      and calling it a repair. If the contract ever gains that field, the story
--      that adds it lifts this CHECK.
--
--   2. `mcp_app_behavior` STAYS UNREACHABLE, but for its OWN reason from now on.
--      Until today it was unreachable by accident: `ck_..._mcp_needs_evidence`
--      requires a render_ref for a `pass`, and `ck_..._render_unpinned` forbade
--      any render_ref. Lifting the second would have silently made the first
--      satisfiable -- and Story 50.6, which delivers the MCP data/render tool
--      split that this dimension actually judges, is still `ready-for-dev`.
--      So the accident is replaced by a rule that names its cause. This is the
--      same correction migration 157 applied to the finalization guard: a
--      constraint must be keyed on the thing it is about, or it blocks the wrong
--      future.
--
-- FOREIGN KEY SHAPES, and why they differ. The four evidence tables reference
-- `app.renders` by COMPOSITE key (id, org_id, project_id): a Render belongs to a
-- Project, and a bare id would let a row point across Projects even when the
-- application layer is wrong -- the standard migration 151 set and 153 followed.
-- `evaluation_runs.render_runtime_version` references `renderer_runtime_builds(id)`
-- by a PLAIN key, and that is not an inconsistency: a runtime build carries no
-- org_id and no project_id because it is one artifact for the whole platform.
-- Scoping a FK to columns the target does not have is not stricter, it is broken.
--
-- NOTHING HERE BACKFILLS. Existing rows keep their NULL and keep reporting their
-- recorded absence: they were produced when it was true. Rewriting immutable
-- evidence to look better in hindsight is exactly what this epic exists to prevent.

-- ---------------------------------------------------------------------------
-- 1. The environment pin: which renderer runtime was available during the run.
-- ---------------------------------------------------------------------------

ALTER TABLE app.evaluation_runs
    DROP CONSTRAINT ck_evaluation_runs_render_unpinned;

ALTER TABLE app.evaluation_runs
    ADD CONSTRAINT fk_evaluation_runs_render_runtime
    FOREIGN KEY (render_runtime_version)
    REFERENCES app.renderer_runtime_builds (id);

COMMENT ON COLUMN app.evaluation_runs.render_runtime_version IS
    'The renderer runtime build available during this execution (Story 50.5). '
    'NULL is still legal and still means unverifiable: a run whose subject has no '
    'rendered artifact pins nothing here. Migration 157 keyed the finalization '
    'guard on this column precisely so that lifting the old CHECK would be enough, '
    'with no trigger to edit.';

-- ---------------------------------------------------------------------------
-- 2. The four evidence pins: the Render this row is actually about.
-- ---------------------------------------------------------------------------

ALTER TABLE app.evaluation_run_cases
    DROP CONSTRAINT ck_evaluation_run_cases_render_unpinned;

ALTER TABLE app.evaluation_run_cases
    ADD CONSTRAINT fk_evaluation_run_cases_render
    FOREIGN KEY (render_ref, org_id, project_id)
    REFERENCES app.renders (id, org_id, project_id);

ALTER TABLE app.observed_cohort_members
    DROP CONSTRAINT ck_observed_cohort_members_render_unpinned;

ALTER TABLE app.observed_cohort_members
    ADD CONSTRAINT fk_observed_cohort_members_render
    FOREIGN KEY (render_ref, org_id, project_id)
    REFERENCES app.renders (id, org_id, project_id);

-- `ck_observed_cohort_members_render_pin` is untouched and only now becomes
-- meaningful: it ties `render_ref IS NULL` to `render_evidence_state =
-- 'unverifiable'`. While the pin was unfillable that equivalence had exactly one
-- satisfiable side.

ALTER TABLE app.feedback_annotations
    DROP CONSTRAINT ck_feedback_annotations_render_unpinned;

ALTER TABLE app.feedback_annotations
    ADD CONSTRAINT fk_feedback_annotations_render
    FOREIGN KEY (render_ref, org_id, project_id)
    REFERENCES app.renders (id, org_id, project_id);

ALTER TABLE app.evaluation_case_dimension_verdicts
    DROP CONSTRAINT ck_evaluation_case_verdicts_render_unpinned;

ALTER TABLE app.evaluation_case_dimension_verdicts
    ADD CONSTRAINT fk_evaluation_case_verdicts_render
    FOREIGN KEY (render_ref, org_id, project_id)
    REFERENCES app.renders (id, org_id, project_id);

-- ---------------------------------------------------------------------------
-- 3. `mcp_app_behavior` stays unreachable -- now for its own stated reason.
-- ---------------------------------------------------------------------------

ALTER TABLE app.evaluation_case_dimension_verdicts
    ADD CONSTRAINT ck_evaluation_case_verdicts_mcp_evaluator_absent
    CHECK (dimension <> 'mcp_app_behavior' OR verdict <> 'pass');

COMMENT ON CONSTRAINT ck_evaluation_case_verdicts_mcp_evaluator_absent
    ON app.evaluation_case_dimension_verdicts IS
    'Story 50.6 (MCP data/render tool split) is ready-for-dev, so the behaviour '
    'this dimension judges cannot be observed and a pass would be a claim about '
    'nothing. Dropped by the story that delivers the evaluator -- and by no other, '
    'because no other makes it false.';

-- ---------------------------------------------------------------------------
-- 4. What is still held NULL, and why it is not a delivery gap.
-- ---------------------------------------------------------------------------

COMMENT ON COLUMN app.golden_question_versions.expected_render_ref IS
    'Held NULL by ck_golden_question_versions_render_unpinned, and NOT because the '
    'Render object is missing -- it exists since Story 50.4/50.5. The ratified '
    'Golden Question contract (analyze-and-test.md:249-259) declares seven required '
    'fields and an expected rendered artifact is not one of them: a Golden Question '
    'is a specification, a Render is produced by a run. Filling this would invent a '
    'product decision. The story that adds the field to the contract lifts the CHECK.';
