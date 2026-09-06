-- 276 -- A red health names the pull that raised it (AI-302).
--
-- WHAT COST FIVE DAYS, TWICE. `app.connection_health.status = 'populate_failed'`
-- is sticky by design (migration 007, review-3-5 F-1): the Nango poll only knows
-- auth state and must not clear a data red flag. And the enqueue gate refuses
-- every pull of an authorization whose health is neither `ok` nor `stale`.
-- One false verdict therefore closes every Datastream behind one credential.
--
-- It happened on 2026-08-12 and again on 2026-08-17, and both times the row said
-- only `populate_failed` -- no pull, no date, no verdict. Reading it answered
-- "this authorization is red" and nothing about WHICH collection made it so, so
-- the only way back to the cause was to correlate a status with no timestamp of
-- its own against the pull ledger by hand. `last_checked_at` is not that date:
-- the poller rewrites it every cycle, so it says when health was last LOOKED at,
-- not when the red was RAISED.
--
-- THREE COLUMNS, AND EACH ANSWERS A QUESTION THE OPERATOR ACTUALLY ASKS:
--
--   populate_failed_pull_id  -- which collection filed the verdict that closed
--                               the door. This is the identifier every other
--                               ledger of the platform is keyed by
--                               (`app.pull_jobs.pull_id`,
--                               `app.pull_verifications.pull_id`), so the row
--                               becomes joinable to the evidence instead of
--                               being a dead end.
--   populate_failed_verdict  -- `empty` or `partial`. Two different facts: a
--                               window that landed nothing, and one that landed
--                               too little. They are not the same repair.
--   populate_failed_at       -- WHEN the red was raised, which `last_checked_at`
--                               cannot say because the poller overwrites it.
--
-- NO FOREIGN KEY ON `populate_failed_pull_id`, AND THAT IS DELIBERATE.
-- `verification._set_connection_health_red` never raises -- it is an annotation,
-- not job control (HG-1). A referential failure there would be swallowed by its
-- own `except` and the red flag would simply NOT BE RAISED: a pull that landed
-- nothing would look healthy. Trading a real red flag for referential tidiness
-- is the wrong way round, so the column is a diagnostic label. Nothing dangles
-- for long anyway: the row is removed with its credential
-- (`connection_ref_id ... ON DELETE CASCADE`, migration 005), which is the path
-- org erasure takes.
--
-- NO CHECK CONSTRAINT TYING THE COLUMNS TO `status = 'populate_failed'`, for the
-- same reason and it is worth writing down. Every writer of this table nulls the
-- three columns whenever the resulting status is not `populate_failed`
-- (`verification.clear_connection_health_red`, `health_poller._upsert_health`,
-- `connections_api` refresh) and a test holds each of them. A CHECK would turn a
-- forgotten null into a raised exception inside code contracted never to raise --
-- i.e. into a LOST health transition, which is worse than a stale label.
--
-- NOTHING IS ERASED AND NO ROW IS REWRITTEN. Three nullable columns are added;
-- every existing row keeps its bytes and reads NULL, which is the truth about it:
-- the reds raised before today never recorded their pull, and this migration
-- cannot invent one.

BEGIN;

ALTER TABLE app.connection_health
    ADD COLUMN IF NOT EXISTS populate_failed_pull_id TEXT,
    ADD COLUMN IF NOT EXISTS populate_failed_verdict TEXT,
    ADD COLUMN IF NOT EXISTS populate_failed_at      TIMESTAMPTZ;

COMMENT ON COLUMN app.connection_health.populate_failed_pull_id IS
    'The pull whose verification verdict raised the current populate_failed red. '
    'NULL whenever status is not populate_failed, and on reds raised before '
    'migration 276. Not a foreign key on purpose -- see the migration header.';
COMMENT ON COLUMN app.connection_health.populate_failed_verdict IS
    'The verdict that raised the red: empty (nothing landed) or partial (too '
    'little landed). NULL whenever status is not populate_failed.';
COMMENT ON COLUMN app.connection_health.populate_failed_at IS
    'When the red was raised. Distinct from last_checked_at, which the health '
    'poller rewrites every cycle and which therefore cannot date the red.';

COMMIT;
