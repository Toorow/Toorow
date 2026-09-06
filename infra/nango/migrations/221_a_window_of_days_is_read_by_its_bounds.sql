-- 221_a_window_of_days_is_read_by_its_bounds.sql
--
-- READING A DATASTREAM BY DAY MAY NOT RE-READ ITS WHOLE HISTORY. Story 58.1,
-- epic 58.
--
-- MEASURED, and the reason this migration exists. The day-grain extract ledger
-- (`server/core/extract_ledger.py`) asks one question of `app.pull_jobs`: which
-- windows of THIS stream OVERLAP the window I am reading --
--
--     WHERE pj.datastream_id = %s
--       AND pj.date_from <= %s::date
--       AND pj.date_to   >= %s::date
--
-- With only `idx_pull_jobs_datastream_id` (migration 023) available, the plan
-- was, under `enable_seqscan = off`:
--
--     Index Scan using idx_pull_jobs_datastream_id on pull_jobs pj
--       Index Cond: (datastream_id = ...)
--       Filter: ((date_from <= ...) AND (date_to >= ...))
--
-- Both date bounds are a HEAP filter: every read of a 35-day strip fetched every
-- pull job the stream ever had and threw most of them away in Postgres. A
-- nightly stream reaches 730 of them in two years, and nothing in the route's
-- statement count moves while that happens -- the count is honest and the work
-- is not. It is the same defect migration 219 closed for the polled progress
-- read, on the same table, for a different question.
--
-- WHAT THIS INDEX BUYS, measured on the disposable cluster with the index
-- created and then rolled back:
--
--     Index Scan using idx_pull_jobs_datastream_dates on pull_jobs pj
--       Index Cond: ((datastream_id = ...) AND (date_from <= ...)
--                    AND (date_to >= ...))
--
-- Honestly: only `date_from <= end` bounds the SCAN. `date_to >= start` is
-- evaluated inside the index and removes the heap fetch for every older window
-- of the stream -- which is what each read pays for today. That is the whole
-- gain, and it is worth its one index.
--
-- NO GiST RANGE INDEX. An overlap test is what `daterange(date_from, date_to)`
-- with `&&` is built for, and no measurement here asks for it yet: this table
-- was 47 rows on the disposable cluster on 2026-08-06 and 6 rows on preprod on
-- 2026-08-06. A range index would also need an expression or a generated column,
-- which is a schema change made on a guess. When a measurement asks, it is its
-- own story.
--
-- `idx_pull_jobs_datastream_id` (023) and `idx_pull_jobs_datastream_completed`
-- (219) are both kept: "every job of this stream" and "what this stream finished
-- most recently" are two other questions, and this one answers neither.
--
-- NOT CONCURRENTLY, deliberately: every migration in this directory is applied
-- inside a transaction, and this table is a queue registry of tens of rows.

CREATE INDEX IF NOT EXISTS idx_pull_jobs_datastream_dates
    ON app.pull_jobs (datastream_id, date_from, date_to);

COMMENT ON INDEX app.idx_pull_jobs_datastream_dates IS
    'Story 58.1: bounds the day-grain extract ledger read by the WINDOW it was '
    'asked for. Without it both date bounds are a heap filter and every read of '
    'a strip of days re-reads every pull job the Datastream ever had.';
