-- 220_a_window_that_never_started_can_be_refused.sql
--
-- STOPPING A RUN REFUSES WHAT HAS NOT STARTED. Story 63.6, epic 63.
--
-- WHAT CAN ACTUALLY BE STOPPED, MEASURED. `queue._execute_job` calls the pull
-- synchronously and consults nothing between two pages; the only mechanism that
-- takes a running job back is `recover_stale_running_jobs`, after
-- `QUEUE_RUNNING_VISIBILITY_SECONDS` -- default 5400. So a provider call already
-- in flight CANNOT be interrupted, and the honest scope of the gesture is the
-- windows still sitting in `queued`. Taking a row out of `queued` is enough for
-- BOTH queue backends: the local poller claims `state = 'queued'` only, and a
-- Cloud Tasks push claims through `claim_job_by_id`, which claims `'queued'`
-- only too and answers `already_terminal` in 200 -- the task is dropped without
-- a retry.
--
-- WHY A NEW NAME AND NOT ONE OF THE SIX. Two states already exist that a lazy
-- author would reach for, and both would be wrong in a way that costs work:
--
--   * `failed` / `dead_letter` -- `_reschedule_failed_pulls` (story 57.8) reads
--     exactly these two and writes `next_run_at = NOW() + interval '1 hour'`
--     with `retry_count + 1`. Marking a stopped window `failed` would RESTART,
--     within the hour, the run a person had just stopped. A stop that undoes
--     itself is the worst possible outcome of the gesture.
--   * `superseded` (migration 022) -- already means "a newer active job took
--     this window over", written by the dedup index. Confounding an automatic
--     replacement with a deliberate stop would leave neither readable.
--
-- WHAT THIS MIGRATION IS NOT. It adds no column, no table and no trigger: the
-- stopped window is a state of a row that already exists, so there is no new
-- append-only table owing an RGPD erasure hatch and no new edge for
-- `org_purge`. `uq_pull_jobs_active` is untouched on purpose -- its predicate is
-- `state IN ('queued','running')`, so a refused window LEAVES the index and the
-- same days can be re-queued, which is exactly what "stopping keeps what was
-- collected and gives the rest back" has to mean.
--
-- The registry `server/core/pull_job_states.py` is compared against this CHECK,
-- entry by entry, by `server/tests/conformance/test_pull_job_state_registry.py`.

BEGIN;

ALTER TABLE app.pull_jobs
    DROP CONSTRAINT IF EXISTS pull_jobs_state_check;

ALTER TABLE app.pull_jobs
    ADD CONSTRAINT pull_jobs_state_check
    CHECK (state IN ('queued','running','done','failed','dead_letter','superseded','cancelled'));

COMMENT ON COLUMN app.pull_jobs.state IS
    'Story 63.6: `cancelled` is a window a PERSON refused before it started. It '
    'is deliberately outside (done, failed, dead_letter) so the catch-up sweep '
    'of story 57.8 cannot restart the run that was just stopped, and outside '
    '`superseded` so a deliberate stop is not read as a dedup replacement. '
    'One owner: server/core/pull_job_states.py.';

COMMIT;
