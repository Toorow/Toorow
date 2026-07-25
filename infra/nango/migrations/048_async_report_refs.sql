-- infra/nango/migrations/048_async_report_refs.sql
--
-- Story 26.1 (A): persistence for the generic async report flow
-- (core/async_reports.py: submit -> poll -> download).
--
-- Some reporting APIs generate reports asynchronously over minutes to hours.
-- The core driver persists the provider's opaque report reference keyed by
-- (ledger_ref, request_hash) so that:
--   * a pull interrupted mid-generation (deadline deferral, worker restart,
--     crash) RESUMES BY RE-POLLING the persisted reference on the next run --
--     NEVER a blind resubmission (epic-26 invariant; some providers hard-fail
--     duplicate in-flight submissions, all of them waste quota on one);
--   * SUBMISSION IS CLAIM-GATED (review 26.1 F-1/F-2): before calling the
--     provider, the driver INSERTs a state='submitting' row (report_ref NULL)
--     with ON CONFLICT DO NOTHING; rowcount=0 means another run owns the row
--     and the driver re-polls or defers -- two concurrent runs can never
--     double-submit. After a successful submit the same row is UPDATEd with
--     the reference and state='in_flight'. An orphaned 'submitting' row
--     (owner crashed) is taken over after ASYNC_REPORT_CLAIM_TTL_SECONDS;
--   * a resubmission after an expired reference / dead download link is
--     counted (resubmit_count) and bounded to ONE before the run fails typed.
--
-- GENERIC per AD-2: ledger_ref (caller scope, e.g. a connection_ref id),
-- request_hash (module-computed hash of the full request shape) and
-- report_ref (provider reference) are ALL opaque strings; core stores and
-- compares them but never interprets them. No provider vocabulary here.
--
-- ONE living row per (ledger_ref, request_hash): state transitions and
-- resubmissions UPSERT the same row (trail in worker logs, not a history table).
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/048_async_report_refs.sql
--
-- Schema-Change-Checklist: additive & idempotent (IF NOT EXISTS everywhere;
-- the whole file is replayable). New table only -- no existing object touched.
-- Objects live in schema app (Postgres is the sole writer -- AD-8). This is
-- migration 048 (047 = dataset_access_grants is the last one in the tree).
--
-- PENDING: NOT applied to Supabase (human-gated, pattern of 25.5's migration 046).
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS app.async_report_refs (
    id              TEXT        PRIMARY KEY,          -- prefixed ULID: 'aref_'
    ledger_ref      TEXT        NOT NULL,             -- opaque caller scope (e.g. connection_ref id)
    request_hash    TEXT        NOT NULL,             -- module-computed hash of the full request shape
    report_ref      TEXT,                             -- opaque provider report reference;
                                                      -- NULL while state='submitting' (claim row,
                                                      -- review 26.1 F-1/F-2) or on a released claim
    state           TEXT        NOT NULL
                    DEFAULT 'submitting'
                    CHECK (state IN ('submitting', 'in_flight', 'completed', 'failed')),
    resubmit_count  INTEGER     NOT NULL DEFAULT 0,   -- bounded to 1 by core (typed failure beyond)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ONE living row per (scope, request shape): the resume/dedup key.
CREATE UNIQUE INDEX IF NOT EXISTS uq_async_report_refs_scope
    ON app.async_report_refs (ledger_ref, request_hash);

-- Review 26.1 F-12: serves (a) the stale-'submitting' claim takeover scan
-- (state + updated_at age) and (b) a future retention job -- terminal rows
-- (completed/failed) carry no resume value and can be purged by
-- updated_at age (suggested: > 30 days); no retention job exists yet, the
-- index makes it cheap when it does.
CREATE INDEX IF NOT EXISTS ix_async_report_refs_state_updated
    ON app.async_report_refs (state, updated_at);

COMMENT ON TABLE app.async_report_refs IS
    'Story 26.1 (A): persisted provider report references for the generic async report flow (core/async_reports.py). One living row per (ledger_ref, request_hash); an in_flight row makes a re-run RE-POLL instead of resubmitting (never a blind resubmission). Generic per AD-2: every stored value is an opaque string core never interprets.';
COMMENT ON COLUMN app.async_report_refs.request_hash IS
    'Module-computed deterministic hash of the FULL request shape (report type, columns, window, account scope, ...). The dedup/resume key together with ledger_ref.';
COMMENT ON COLUMN app.async_report_refs.state IS
    'submitting (claim row: submit right acquired, provider reference not yet persisted -- report_ref NULL; orphans taken over after ASYNC_REPORT_CLAIM_TTL_SECONDS) | in_flight (submitted, generation pending -- a re-run re-polls) | completed (downloaded) | failed (provider failure, second expiry after the single allowed resubmission, or released claim after a submit error).';

COMMIT;
