-- 297_a_refused_pull_is_not_an_empty_one.sql
--
-- A WINDOW THE SOURCE DID NOT ALLOW TO RUN. AI-307,
-- docs/product-architecture/execution-substrate.md.
--
-- THE FALSE NUMBER THIS CLOSES. `queue._execute_job` read one key of the pull
-- envelope -- `result.get("row_count", 0)` -- and wrote
-- `_finish_job(conn, job, DONE, row_count=row_count)`. A pull the provider
-- refused BEFORE it ever ran (a Business Profile project still at 0 QPM, a
-- legacy host the project is not allowlisted on, a scope in an approval queue)
-- therefore produced `done / 0 row`: letter for letter the row written by a pull
-- that ran and honestly found nothing. Nobody could tell the two apart.
--
-- AND THE ZERO DID NOT STOP THERE. `done / 0` publishes `FACT_PULL_LANDED` with
-- `row_count: 0`; the verification subscriber counts the pull's rows, finds
-- none, and files verdict `empty`; `empty` raises `app.connection_health.status
-- = 'populate_failed'`, which is STICKY (migration 007) and which the enqueue
-- gate reads. One quota Google has not granted yet therefore refused every
-- Datastream behind the whole authorization -- the same closure AI-302 measured
-- twice in production, arriving through the door AI-302 did not cover.
--
-- WHY A SEVENTH NAME AND NOT ONE OF THE SIX. Every existing name is wrong here
-- in a way that costs work:
--
--   * `done`        -- the defect itself, and the one that closes the door.
--   * `failed` / `dead_letter` -- `scheduler._reschedule_failed_pulls` (story
--     57.8) reads exactly those two and writes `next_run_at = NOW() + interval
--     '1 hour'`. A grant a human at the provider has to approve will not have
--     landed an hour later: that is an hourly crash loop against an approval
--     queue, not a retry.
--   * `cancelled` (migration 220) -- already means "a PERSON refused this window
--     before it started". Nobody chose here; that is the whole point.
--   * `superseded` (migration 022) -- already means "a newer active job took
--     this window over".
--
-- WHAT THIS MIGRATION IS NOT. It adds no column, no table and no trigger: a
-- prevented window is a state of a row that already exists, so there is no new
-- append-only table owing an RGPD erasure hatch and no new edge for `org_purge`.
-- `uq_pull_jobs_active` is untouched on purpose -- its predicate is
-- `state IN ('queued','running')`, so a prevented window LEAVES the index and
-- the very same days can be re-asked the moment the grant lands, which is what
-- "the message names the gesture that repairs" has to mean in the data.
--
-- `row_count` IS LEFT NULL BY THE WRITER, and that is the load-bearing half.
-- `0` is a count that was taken; a prevented window took none. `_finish_job` is
-- called with `row_count=None`, so the column keeps its NULL and no reader can
-- add a zero into a total it did not measure.
--
-- The registry `server/core/pull_job_states.py` is compared against this CHECK,
-- entry by entry, by `server/tests/conformance/test_pull_job_state_registry.py`.

BEGIN;

ALTER TABLE app.pull_jobs
    DROP CONSTRAINT IF EXISTS pull_jobs_state_check;

ALTER TABLE app.pull_jobs
    ADD CONSTRAINT pull_jobs_state_check
    CHECK (state IN ('queued','running','done','failed','dead_letter','superseded','cancelled','prevented'));

COMMENT ON COLUMN app.pull_jobs.state IS
    'AI-307: `prevented` is a window the SOURCE did not allow to run -- an '
    'un-granted quota, an allowlist the project is not on, a scope still in an '
    'approval queue. It is deliberately outside `done` so a refused pull can '
    'never be read as an empty one (and can never file the `empty` verdict that '
    'raises the sticky populate_failed), and outside (failed, dead_letter) so '
    'the catch-up sweep of story 57.8 cannot retry it every hour against a grant '
    'only a human at the provider can give. `cancelled` stays a window a PERSON '
    'refused before it started. One owner: server/core/pull_job_states.py.';

COMMIT;
