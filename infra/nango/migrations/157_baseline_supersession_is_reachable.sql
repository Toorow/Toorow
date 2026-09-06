-- Story 51.2 repair: a profile could never receive a SECOND baseline.
--
-- WHAT WAS BROKEN. Migration 153 stated the rule correctly -- "a baseline is
-- superseded, never repointed" -- and then made the successor impossible to
-- create. Two guards were each right on their own and deadlocked together:
--
--   * `uq_evaluation_baseline_active`, a partial UNIQUE index on
--     (run_profile_id) WHERE superseded_by_baseline_id IS NULL, so at most one
--     baseline per profile is active. Correct, and worth keeping.
--   * `fk_evaluation_baselines_superseded`, a self-referencing foreign key that
--     was NOT DEFERRABLE.
--
-- The two orders a caller can try are the only two that exist, and both are
-- refused. Measured against a live PostgreSQL 17 before writing this file:
--
--     insert the successor while the incumbent is active  -> UniqueViolation
--     supersede the incumbent toward a successor that does
--     not exist yet                                       -> ForeignKeyViolation
--
-- There is no third order, so the first baseline approved for a profile was
-- also the last one it could ever have. The defect is not that the rule is too
-- strict; it is that the rule as encoded had no legal path through it.
--
-- WHY THIS IS THE SAME MISTAKE AS THE FINALIZATION GUARD, repaired in the same
-- delivery: a constraint that is satisfiable in the state the product is in
-- today, and unsatisfiable in the state the product is meant to reach. Both
-- were written by reasoning about the first row rather than about the second.
--
-- THE REPAIR, and why it is this one. Deferring the self-FK to COMMIT gives the
-- caller exactly one legal path and keeps every guarantee:
--
--     BEGIN;
--       UPDATE ... SET superseded_by_baseline_id = <successor id>  -- incumbent
--       INSERT ... (<successor id>, ...)                           -- successor
--     COMMIT;   -- the FK is verified here, and it holds
--
-- The partial unique index is untouched: between the UPDATE and the COMMIT the
-- incumbent is no longer active, so the successor does not collide, and at COMMIT
-- exactly one active baseline per profile remains -- which is the invariant the
-- index exists to hold. Nothing here lets a baseline ADVANCE on its own: the
-- supersession is still an explicit, approved write, `run_id` is still immutable
-- (`trg_evaluation_baselines_supersede_only`), and DELETE is still refused.
--
-- Deferring only affects WHEN the reference is checked, never WHETHER: a
-- transaction that supersedes toward an id it never inserts is rolled back at
-- COMMIT, exactly as it is refused today.

ALTER TABLE app.evaluation_baselines
    DROP CONSTRAINT fk_evaluation_baselines_superseded;

ALTER TABLE app.evaluation_baselines
    ADD CONSTRAINT fk_evaluation_baselines_superseded
    FOREIGN KEY (superseded_by_baseline_id, org_id, project_id)
    REFERENCES app.evaluation_baselines (id, org_id, project_id)
    DEFERRABLE INITIALLY DEFERRED;

COMMENT ON CONSTRAINT fk_evaluation_baselines_superseded ON app.evaluation_baselines IS
    'DEFERRABLE INITIALLY DEFERRED on purpose (migration 157): superseding the '
    'incumbent and inserting its successor must happen in one transaction, and '
    'neither order works when the check is immediate. Making it immediate again '
    'makes a second baseline unreachable for every profile.';
