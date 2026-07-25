-- infra/nango/migrations/046_account_topology.sql
--
-- Story 25.5: Account topology & onboarding scoping (AC2).
--
-- The onboarding flow (discovery -> selection -> access check -> trial ->
-- windowed backfill) needs a single, per-credential record of WHICH reporting
-- account a connection is scoped to, and whether that account has been access-
-- verified. This table is that record. It is deliberately GENERIC (AD-2: no
-- provider vocabulary) -- account_id / account_label are opaque strings a
-- connector's discovery call resolved; core never interprets them.
--
-- STATE MACHINE (AC2):
--   'pending_account_selection'  -- a scope row may exist as a placeholder (or be
--                                   absent entirely) before the user picks an account.
--   'ready'                      -- an account was selected AND access-verified; the
--                                   enqueue guard (AC4) allows pulls only in this state.
-- A pull can NEVER run against an unselected/unverified account: enqueue_pull
-- refuses when the provider declares account_topology and no 'ready' row exists.
--
-- ONE scope per credential: keyed UNIQUE on connection_ref_id. Selecting a new
-- account UPSERTs this single living row (re-scoping is an explicit re-selection,
-- not a history table -- the audit_log carries the trail, AD-8/AD-14).
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/046_account_topology.sql
--
-- Schema-Change-Checklist: additive & idempotent (IF NOT EXISTS everywhere;
-- the whole file is replayable). New table only -- no existing object touched.
-- Objects live in schema app (Postgres is the sole writer -- AD-8). This is
-- migration 046 (045 = media_plan_import_contracts is the last applied one).
--
-- PENDING: NOT applied to Supabase (human-gated, pattern of 22.6's migration 044).
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- ---------------------------------------------------------------------------
-- app.connection_account_scope -- the selected + verified reporting account for
-- one credential (connection_ref). Generic: account_id / account_label are opaque
-- strings resolved by the connector's discovery call (AD-2, no provider vocabulary).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.connection_account_scope (
    id                 TEXT        PRIMARY KEY,        -- prefixed ULID: 'ascope_'
    connection_ref_id  TEXT        NOT NULL
                       REFERENCES app.connection_ref(id) ON DELETE CASCADE,
    account_id         TEXT,                           -- opaque selected account id (NULL while pending)
    account_label      TEXT,                           -- human-readable label (best effort)
    state              TEXT        NOT NULL
                       DEFAULT 'pending_account_selection'
                       CHECK (state IN ('pending_account_selection', 'ready')),
    verified_at        TIMESTAMPTZ,                    -- when access was last verified (NULL until 'ready')
    selected_by        TEXT,                           -- identity subject that selected the account (AD-14)
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ONE living scope per credential: selection UPSERTs on this key.
CREATE UNIQUE INDEX IF NOT EXISTS uq_connection_account_scope_connection
    ON app.connection_account_scope (connection_ref_id);

-- The enqueue guard (AC4) probes state='ready' by connection; index it.
CREATE INDEX IF NOT EXISTS idx_connection_account_scope_state
    ON app.connection_account_scope (connection_ref_id, state);

COMMENT ON TABLE app.connection_account_scope IS
    'Story 25.5 (AC2): the selected + access-verified reporting account for one credential (connection_ref). State machine pending_account_selection -> ready; a pull runs only when a ready row exists (enqueue guard AC4). Generic per AD-2: account_id / account_label are opaque strings a connector discovery call resolved -- core never interprets them. One living row per connection (UPSERT on re-selection); the trail lives in app.audit_log.';
COMMENT ON COLUMN app.connection_account_scope.account_id IS
    'Opaque selected account id (e.g. a GA4 property id like properties/123456). NULL while pending_account_selection. Core never parses this value (AD-2).';
COMMENT ON COLUMN app.connection_account_scope.state IS
    'pending_account_selection (no verified account yet) | ready (account selected AND access-verified). enqueue_pull refuses to enqueue a topology-declaring connection unless a ready row exists.';

COMMIT;
