-- infra/nango/migrations/081_dataset_rollback.sql
--
-- Story 12.12: replace, append, and roll back a dataset safely.
--
-- SCOPE OF THIS MIGRATION (additive + idempotent after 042 / 060 / 073):
-- It adds ONLY the retention / rollback-deadline concept that Story 12.5's
-- candidate registry (migration 042) does not yet carry, so the DATASET-pointer
-- rollback path (server/core/dataset_recovery.py) can:
--   1. decide whether a prior published execution is still a VALID rollback
--      TARGET (retained + within the rollback window), and
--   2. record a rollback as a NEW append-only publication-log row without ever
--      mutating history (the 042 immutability trigger stays authoritative).
--
-- WHAT IT DOES NOT DO (reuse, never rebuild):
--   * It does NOT create a second publication engine or a second pointer. The
--     dataset pointer stays app.datastreams.current_published_execution_id (042);
--     the rollback re-uses commit_publication's atomicity contract exactly (a
--     locked FOR UPDATE pointer swap + append-only log row in ONE transaction).
--   * It does NOT touch the MAPPING-pointer rollback of Story 36.18
--     (app.datastreams.current_mapping_version_id /
--      app.datastream_mapping_publication_log, migration 073). That is a DISTINCT
--     concern; this migration is exclusively about the EXECUTION (dataset) pointer.
--   * It adds NO provider/source vocabulary (AD-2).
--
-- Apply with (human-gated on Jean's authorization -- Supabase only):
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/081_dataset_rollback.sql

BEGIN;

-- ---------------------------------------------------------------------------
-- 1) Rollback-deadline / retention on the EXISTING append-only publication log.
--
-- app.datastream_publication_log (migration 042) already records every forward
-- publication WITH the prior pointer (prior_execution_id) -- the rollback
-- EVIDENCE. Story 12.12 adds the WINDOW during which the execution that this log
-- row published may still be rolled BACK TO (i.e. re-pointed to as a healthy prior
-- version). Both columns are additive + NULL-able so re-application and existing
-- rows are untouched:
--
--   * rollback_deadline : TIMESTAMPTZ NULL -- an OPTIONAL explicit wall-clock instant
--       after which this published execution may no longer be a rollback target. The
--       reused 12.5 commit_publication forward-publish path leaves this NULL on every
--       normal publish; NULL means "the runtime resolves the EFFECTIVE deadline from
--       published_at + the project-scoped window" via
--       COALESCE(rollback_deadline, published_at + resolved_window) in
--       dataset_recovery.py (never a silent platform-wide hardcode -- the recovery
--       result records deadline_source, and it FAILS CLOSED when neither a stored
--       deadline nor a resolvable published_at exists). A rollback attempt past the
--       effective deadline is DISABLED with a clear explanation (AC: expired deadline).
--   * retained : BOOLEAN NOT NULL DEFAULT TRUE -- whether the published dataset
--       behind this execution is still physically retained and therefore a SAFE
--       rollback target. A retention/TTL sweep (out of scope here) flips this to
--       FALSE; the rollback path then refuses that target (fail closed: never
--       re-point at data that is no longer there).
-- ---------------------------------------------------------------------------
-- NOTE (C2): these are ADD COLUMN IF NOT EXISTS only. Column adds are metadata-only
-- and do NOT fire the 042 BEFORE-UPDATE-OR-DELETE immutability trigger
-- (trg_datastream_publication_log_immutable), so this migration applies cleanly on a
-- POPULATED log. A previous revision here ran a backfill
-- `UPDATE ... SET rollback_deadline = published_at + INTERVAL '30 days'
--  WHERE rollback_deadline IS NULL`. That UPDATE fires the append-only immutability
-- trigger (RAISE EXCEPTION) for EVERY existing row and aborts the whole migration --
-- it only "worked" on an empty table. It is REMOVED. Existing rows keep
-- rollback_deadline = NULL and the runtime resolves the EFFECTIVE deadline from
-- COALESCE(rollback_deadline, published_at + resolved_window) in
-- server/core/dataset_recovery.py (dataset_recovery._load_rollback_target). This also
-- removes the 30-day hardcode that ignored the project rollback_window_hours pref.
ALTER TABLE app.datastream_publication_log
    ADD COLUMN IF NOT EXISTS rollback_deadline TIMESTAMPTZ;

ALTER TABLE app.datastream_publication_log
    ADD COLUMN IF NOT EXISTS retained BOOLEAN NOT NULL DEFAULT TRUE;

-- Efficient "is there a still-rollbackable prior published execution?" lookup:
-- newest-first, retained-only, per datastream.
CREATE INDEX IF NOT EXISTS idx_datastream_publication_log_rollbackable
    ON app.datastream_publication_log (datastream_id, published_at DESC)
    WHERE retained;

-- ---------------------------------------------------------------------------
-- 2) Project-scoped rollback-window preference (additive, mirrors 042's DQ prefs).
--
-- rollback_window_hours : INTEGER NULL -- how long after a publication a rollback
--   remains permitted. NULL => the documented default (30 days == 720 hours) is
--   resolved in dataset_recovery.py, which records deadline_source in the result.
--   0 is permitted and means "no rollback window" (rollback disabled immediately).
-- ---------------------------------------------------------------------------
ALTER TABLE app.project_preferences
    ADD COLUMN IF NOT EXISTS rollback_window_hours INTEGER;

ALTER TABLE app.project_preferences
    DROP CONSTRAINT IF EXISTS chk_project_preferences_rollback_window_hours;
ALTER TABLE app.project_preferences
    ADD CONSTRAINT chk_project_preferences_rollback_window_hours
    CHECK (
        rollback_window_hours IS NULL
        OR rollback_window_hours >= 0
    );

-- ---------------------------------------------------------------------------
-- 3) Append classification on the datastream (Append is UNAVAILABLE by default).
--
-- Story 12.12: Append is UNAVAILABLE unless a stable key + dedup contract +
-- compatible schema exist; when unavailable, Replace is the safe supported action
-- (mirrors 12.8/12.9 replace-only default). We record the append CONTRACT so the
-- unavailability is a data property, not a code guess:
--
--   append_stable_key : JSONB NULL -- the declared stable key column set that makes
--       Append safe (a dedup contract). NULL / empty => Append is UNAVAILABLE and
--       Replace is presented instead. This is intentionally additive + NULL so
--       every existing datastream defaults to replace-only (safe).
-- ---------------------------------------------------------------------------
ALTER TABLE app.datastreams
    ADD COLUMN IF NOT EXISTS append_stable_key JSONB
        CHECK (append_stable_key IS NULL OR jsonb_typeof(append_stable_key) = 'array');

COMMIT;
