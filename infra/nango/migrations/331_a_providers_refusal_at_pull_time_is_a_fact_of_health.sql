-- A provider's refusal at pull time is a fact of health (AI-341).
--
-- THE DEFECT, MEASURED 2026-08-31 ON THE REFERENCE PROJECT. YouTube Analytics
-- answered 403 permission_denied to all 8 Analytics Datastreams of the Kardinal
-- project from 2026-08-26 on (56 failed pull_jobs over 7 days, each carrying
-- `error_class: permission_denied, user_action: reconnect`). The connection's
-- health row read `ok` the whole time -- its last write was the 2026-08-25
-- sweep, BEFORE the failure began -- because:
--
--   [1] the health poll reads LOCAL state only (token blob + expiry for a
--       google_direct row); a provider-side refusal is invisible to it, and
--       `token_service.google_direct_health` says so in its own docstring:
--       "a failed refresh raises auth_expired AT PULL TIME";
--   [2] the pull, which DOES learn the truth and classifies it
--       (queue.py, story 25.2 typed errors), never writes connection_health.
--
-- The instrument that knows does not speak to the instrument people read.
-- G13-T01 read the stale `ok` and passed green through six days of outage.
--
-- THE REPAIR IS THE AUTH TWIN OF AI-302's DATA RED. `populate_failed` already
-- proves the pattern: a red raised where the truth is learned, named after the
-- pull that raised it (migration 276), sticky against the poller, lifted only
-- by the write that proves the opposite. This migration gives the same shape to
-- the provider's refusal:
--
--   * new status `provider_denied` -- the authorization is alive (the token
--     refreshes; the Data API may still answer) but the provider refuses the
--     data this connection exists to collect. Distinct from `revoked` (the
--     authorization itself is dead -- reconnect) and deliberately NOT named
--     `access_denied`: that code belongs to RIGHTS refusals, and the 2026-08-17
--     decision in execution-substrate.md separates the two on purpose.
--   * two columns naming the pull that raised it, nulled by every writer that
--     moves the row off `provider_denied` (the migration 276 rule: a label may
--     never describe a status the row no longer holds).
--
-- Door semantics (written in execution-substrate.md, same commit): unlike
-- `revoked`, `provider_denied` does NOT close the enqueue gate -- the daily
-- probe is the detector of restoration, and a verified `ok` pull lifts the red
-- without any console gesture.

BEGIN;

ALTER TABLE app.connection_health
    DROP CONSTRAINT IF EXISTS connection_health_status_check;

ALTER TABLE app.connection_health
    ADD CONSTRAINT connection_health_status_check
    CHECK (status IN ('ok', 'stale', 'revoked', 'populate_failed', 'provider_denied'));

ALTER TABLE app.connection_health
    ADD COLUMN IF NOT EXISTS provider_denied_pull_id TEXT,
    ADD COLUMN IF NOT EXISTS provider_denied_at TIMESTAMPTZ;

COMMENT ON COLUMN app.connection_health.provider_denied_pull_id IS
    'The pull whose provider refusal (403 permission_denied) raised the '
    'provider_denied status. NULL whenever status is not provider_denied.';
COMMENT ON COLUMN app.connection_health.provider_denied_at IS
    'When that refusal was recorded. NULL whenever status is not provider_denied.';

COMMIT;
