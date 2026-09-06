-- 291: A PLATFORM ALERT BELONGS TO NO PROJECT (AI-306).
--
-- MEASURED IN PRODUCTION, not deduced. Cloud Run, 2026-08-18T00:32:25Z, twice:
--
--   WARNING core.scheduler scheduler: meta_alert_insert_failed step=dbt_per_project:
--   insert or update on table "alert_firings" violates foreign key constraint
--   "fk_alert_firings_project"
--
-- `scheduler._insert_meta_alert` wrote `project_id = 'default'`, a literal from
-- before the multi-project layer. Migration 018 seeded a project of that id and
-- added the foreign key; that row is gone from production, so every platform
-- alert has been refused at write time ever since and the caller's `except`
-- turned each one into a log line. The first real nightly is where it was seen,
-- but nothing about it is new: `scheduler_step_degraded`, the cache-build alert
-- and every `write_infra_firing` caller that did not name a project of its own
-- were failing the same way, silently, for as long as the row has been missing.
--
-- The disposable test database does NOT reproduce it -- migration 018 seeds
-- `'default'` there and the chain gives it an org before migration 100 makes
-- `org_id` NOT NULL -- which is precisely why a local green run said nothing
-- about this, and why the production log is the only thing that decided it.
--
-- WHERE A PLATFORM-SCOPED ALERT LIVES. This repository had already answered it
-- elsewhere and the answer is not invented here: an object that belongs to no
-- Project carries `project_id IS NULL` -- `docs/product-architecture/
-- governance.md`, "a platform-scoped Concept (`project_id IS NULL`)". The
-- nightly scheduler is one process serving every Project; its health is nobody's
-- Project and everybody's concern. So:
--
--   * the column becomes NULLABLE, and NULL means platform scope. A foreign key
--     does not constrain NULL, so the FK stays exactly as it is -- no constraint
--     is dropped, weakened or replaced by this migration;
--   * `business_alerts.fetch_recent_meta_alerts` unions it, as it already
--     unioned the `'default'` sentinel for the same reason ("all projects see
--     scheduler health per the shared-scheduler design"). The sentinel branch
--     stays for a base that still holds such a row.
--
-- AND THE COLUMN DEFAULT GOES, because it is the trap itself. `DEFAULT 'default'`
-- (migration 013) means any INSERT that omits `project_id` aims at a row that
-- does not exist: it does not fail loudly at the writer, it fails at the
-- database, inside somebody's `except`. Measured before removing it: every
-- INSERT into this table in `server/` names `project_id` explicitly except one,
-- `business_alerts`, repaired in the same lot. With no default, an omission
-- lands as NULL -- platform scope, honest and readable -- instead of pointing at
-- a ghost.
--
-- NOTHING TO BACKFILL. Every row this defect concerned was refused at write time
-- and never existed. This migration adds no data and rewrites none.

BEGIN;

ALTER TABLE app.alert_firings ALTER COLUMN project_id DROP NOT NULL;
ALTER TABLE app.alert_firings ALTER COLUMN project_id DROP DEFAULT;

COMMENT ON COLUMN app.alert_firings.project_id IS
    'The Project this firing is about. NULL is PLATFORM SCOPE: a finding about '
    'the shared substrate -- the nightly scheduler, a cache build, a token '
    'revocation -- that belongs to no single Project and is read by all of them '
    '(AI-306, migration 291). The same convention governance objects use. Never '
    'a sentinel string: `default` named a Project row that production does not '
    'have, and the foreign key refused every platform alert for as long as it '
    'was written.';

COMMIT;
