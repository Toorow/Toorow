-- 275 -- A pull belongs to its Datastream, and dedup must say so (AI-302).
--
-- WHAT WAS MEASURED, on production 2026-08-17. Nine active Datastreams share one
-- Google authorization and one 30-day window, and each reads a DIFFERENT
-- `report_profile_id` (channel_daily, video_upload, audience_geography,
-- audience_device, audience_demographics, audience_subscription,
-- playback_locations, traffic_sources, video_daily). One nightly tick dispatched
-- all nine. `app.pull_jobs` received exactly ONE row:
--
--     done  ds=Kardinal - audience by age and gender  rows=344
--
-- The other eight deduplicated into it, because `uq_pull_jobs_active` keyed on
-- (connection_ref_id, date_from, date_to) and NOT on the Datastream. Their eight
-- report profiles were never pulled. Worse, a deduplicated answer is a REAL job
-- (`state='running'`), so `dispatch_windows` called `on_enqueued` for each and
-- all eight advanced `next_run_at` to the next night: work skipped, clock moved,
-- nothing said. Same family as AI-301 -- something that looks collected and is not.
--
-- WHY IT SURVIVED SO LONG. The only pulls this platform ever completed before
-- 2026-08-17 were requested BY HAND (`app.pull_jobs.requested_by` is a person on
-- every row before that date). A human clicks one Datastream, waits, clicks the
-- next: each job reaches `done` before the following one is enqueued, and a
-- terminal row is outside the partial index. Dedup therefore never fired. The
-- first fleet dispatch is what exposed it, and the clock had never run one.
--
-- WHAT THE RULE WAS FOR, AND IT IS KEPT. "Un double-clic est une dépense, pas
-- deux" -- the same Datastream asked twice for the same window is ONE pull. That
-- is preserved exactly: the Datastream joins the key, so a repeat of the same
-- (connection, datastream, window) still collides. What no longer collides is
-- two DIFFERENT Datastreams, which were never the same spend: they read
-- different report profiles and land different rows.
--
-- COALESCE, AND WHY IT IS NOT COSMETIC. `datastream_id` is nullable -- the legacy
-- per-connection dispatch writes none. In a unique index NULLs never collide, so
-- keying on the bare column would SILENTLY STOP deduplicating that path, turning
-- a double-click there into two provider pulls. `COALESCE(datastream_id, '')`
-- keeps every connection-level pull colliding with every other exactly as today.
--
-- NO ROW IS TOUCHED, NOTHING IS ERASED. This is an index swap: the historical
-- rows keep their bytes, and the partial predicate still covers only `queued`
-- and `running`, so no terminal job is affected. `core.org_purge` is not
-- involved -- no column, table or ownership edge changes.
--
-- THE THREE COPIES OF THIS KEY MUST AGREE, and the other two move in the same
-- commit: the dedup fast-path SELECT and the `ON CONFLICT` target, both in
-- `server/core/queue.py`. An index that disagrees with its ON CONFLICT clause
-- raises at INSERT rather than deduplicating, so a divergence is loud -- but only
-- once a second Datastream is in flight, which is precisely the case nobody had
-- ever run.

BEGIN;

DROP INDEX IF EXISTS app.uq_pull_jobs_active;

CREATE UNIQUE INDEX uq_pull_jobs_active
    ON app.pull_jobs (connection_ref_id, COALESCE(datastream_id, ''), date_from, date_to)
    WHERE state IN ('queued', 'running');

COMMIT;
