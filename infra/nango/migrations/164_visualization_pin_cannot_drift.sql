-- Story 50.4 repair (review finding F1): a revision may not silently re-pin a
-- Visualization to a DIFFERENT Query Spec.
--
-- WHAT WAS WRONG, REPRODUCED AGAINST THE DATABASE. `create_visualization_spec_version`
-- read `(current_version_id, query_spec_id)` off the head and used only the first
-- column; the second was fetched and discarded. The INSERT then wrote
-- `validated.query_spec_id`, which is whatever Query Spec the caller's
-- `query_spec_version_id` belongs to. So:
--
--     head.query_spec_id = qs_...KS5 · v1 = qs_...KS5 · v2 = qs_...Q2K   <- accepted
--
-- Version N+1 of a saved presentation answered a DIFFERENT question while
-- `app.visualizations.query_spec_id` still advertised the old one, and every
-- consumer that resolves the head -- a Report's default presentation, a Render
-- pin, Story 50.5's runtime -- got the wrong query.
--
-- WHY THE PYTHON FIX IS NOT ENOUGH. `server/core/visualization_specs.py` now
-- refuses the mismatch with `query_spec_mismatch`, which protects every caller
-- that goes through the service. This migration protects the ones that do not: a
-- psql session, a repair script, a future migration, a second service written by
-- someone who did not read the first. Same reasoning as migration 156's CHECKs and
-- as `ck_query_results_ai_path_is_honest` (`151_query_specs_and_results.sql:239-242`):
-- the database is the layer that cannot be argued with.
--
-- HOW IT IS ENFORCED. The head gains a UNIQUE on `(id, query_spec_id, org_id,
-- project_id)`, and the version table's head foreign key is WIDENED to carry
-- `query_spec_id`. A version row therefore cannot exist unless its
-- `query_spec_id` is the SAME value the head carries -- structurally, without a
-- trigger to bypass and without a CHECK that a NULL could satisfy.
--
-- WHAT THIS DOES NOT FORBID. Re-pinning to a NEW VERSION of the SAME Query Spec.
-- That is the legitimate act AC5 describes: Explore produces `query_spec_versions`
-- row N+1 under the same `query_spec_id`, and the Visualization re-pins to it
-- after full revalidation. Only crossing to another Query Spec is refused.
--
-- MIGRATION 156 IS NOT RE-EDITED. It is applied; an applied migration is corrected
-- by the next one (CLAUDE.md, "Migrations immuables").

-- ---------------------------------------------------------------------------
-- 1. The head can be referenced by (identity, Query Spec, Project).
-- ---------------------------------------------------------------------------
ALTER TABLE app.visualizations
    DROP CONSTRAINT IF EXISTS uq_visualizations_query_spec_scope;
ALTER TABLE app.visualizations
    ADD CONSTRAINT uq_visualizations_query_spec_scope
    UNIQUE (id, query_spec_id, org_id, project_id);

-- ---------------------------------------------------------------------------
-- 2. The version's head reference carries the Query Spec.
--
--    Declared validated, not NOT VALID: an unvalidated constraint is a promise
--    the planner honours and the data does not, which is the same defect class as
--    a CHECK a NULL satisfies (migration 159). If a row already violates it, this
--    ALTER fails loudly and someone reads the rows -- which is the correct
--    outcome, because such a row is a presentation pinned to a question its head
--    does not name.
-- ---------------------------------------------------------------------------
ALTER TABLE app.visualization_spec_versions
    DROP CONSTRAINT IF EXISTS fk_visualization_spec_versions_head;
ALTER TABLE app.visualization_spec_versions
    ADD CONSTRAINT fk_visualization_spec_versions_head
    FOREIGN KEY (visualization_id, query_spec_id, org_id, project_id)
    REFERENCES app.visualizations (id, query_spec_id, org_id, project_id);

-- The widened key indexes (visualization_id, query_spec_id, org_id, project_id);
-- `idx_visualization_spec_versions_head` (156:172) still serves the
-- head-resolution read, so no index is dropped here.

COMMENT ON CONSTRAINT fk_visualization_spec_versions_head
    ON app.visualization_spec_versions IS
    'Story 50.4 F1: a version cannot name a Query Spec other than the one its '
    'Visualization head carries. Re-pinning to a new VERSION of the same Query '
    'Spec stays allowed; crossing to another Query Spec is refused by the key.';
