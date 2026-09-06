-- 219_the_rate_of_a_stream_is_read_bounded.sql
--
-- A POLLED STATEMENT MAY NOT READ A WHOLE HISTORY. Story 63.4, epic 63.
--
-- Story 63.4 estimates how much longer a collection has from the finished runs
-- of the same Datastream, read inside the progress statement -- the one address
-- of the product called in a loop (every 5 s, from three surfaces, story 63.3).
--
-- MEASURED, and the reason this migration exists: with only
-- `idx_pull_jobs_datastream_id` (migration 023) available, the plan was
--
--     Limit <- Sort <- GroupAggregate <- Bitmap Heap Scan on pull_jobs
--
-- The `LIMIT` sits ABOVE the aggregate, so it bounds the ROWS RETURNED and not
-- the rows READ: every tick re-read and sorted every pull job the Datastream has
-- ever had. A nightly stream reaches 730 of them in two years, and the statement
-- count -- which is what the route's cost test measures -- never moved. The
-- instruction count was honest and the work was not.
--
-- WHAT THIS INDEX BUYS. It lets the read be bounded BEFORE the aggregate:
--
--     ... ORDER BY j.completed_at DESC LIMIT 30   -- an index scan, 30 rows
--     GROUP BY execution_id                       -- over those 30 rows only
--
-- 30 is derived, not chosen: the largest window count any path in this
-- repository produces is 3 (`refetch.py:57,196-204`), and the estimate reads 10
-- runs. The predicate matches the query's own filters exactly, so the index is
-- usable while the statement is generically planned -- which is what a statement
-- executed in a loop gets once the driver prepares it.
--
-- AND IT IS THE SAME SHAPE FOR EVERY READER OF THIS TABLE. Anything asking "what
-- did this Datastream collect recently" wants (datastream_id, completed_at DESC)
-- and would otherwise pay the same full scan. `idx_pull_jobs_datastream_id` is
-- kept: it answers "every job of this stream", which is a different question.
--
-- NOT CONCURRENTLY, deliberately: `app.pull_jobs` is a queue registry measured
-- at 6 rows on preprod on 2026-08-06, and every migration in this directory is
-- applied in a transaction.

-- `state = 'done'` is in the predicate on purpose: it is the query's own filter,
-- and leaving it out made the planner prefer the older index plus a sort,
-- because every row it returned still had to be re-checked against the heap.
-- The literal is the same one migration 022's CHECK already fixes.
CREATE INDEX IF NOT EXISTS idx_pull_jobs_datastream_completed
    ON app.pull_jobs (datastream_id, completed_at DESC)
    WHERE state = 'done'
      AND execution_id IS NOT NULL
      AND completed_at IS NOT NULL
      AND started_at IS NOT NULL;

COMMENT ON INDEX app.idx_pull_jobs_datastream_completed IS
    'Story 63.4: bounds the per-day rate read of the progress route BEFORE its '
    'aggregate. Without it the polled statement re-sorts every pull job of the '
    'Datastream on every 5-second tick.';
