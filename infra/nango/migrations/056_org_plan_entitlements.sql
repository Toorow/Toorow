-- infra/nango/migrations/056_org_plan_entitlements.sql
--
-- Story 34.1: Org plan & entitlements -- fondation gouvernance des essais (Epic 34).
--
-- Two tables:
--   * app.org_plan         -- current plan per org (mutable: plan can change over time).
--   * app.org_plan_history -- append-only audit trail: every change to org_plan writes
--                             one row here; rows are NEVER updated or deleted.
--
-- STRICTLY ADDITIVE & PASSIVE: this migration creates tables only. It wires NOTHING
-- onto the runtime. Enforcement guards (max_backfill_days, max_datastreams) land in
-- Stories 34.2/34.3. Zero impact on existing rows and connectors.
--
-- Plan enum: 'trial' | 'full' | 'internal'.
--   trial    -- default for every new org (30 days backfill, 3 datastreams).
--   full     -- paid, unlimited.
--   internal -- toorow-internal orgs, unlimited (same resolution as full).
--
-- Entitlements source of truth: DEFAULT_TRIAL_ENTITLEMENTS in server/core/org_entitlements.py.
-- The SQL DEFAULT 'trial' and the CHECK enum MUST stay in sync with that constant.
--
-- Append-only pattern (003/004): REVOKE UPDATE/DELETE alone is ineffective against the
-- table OWNER. We reproduce the house pattern of 049 (metric_semantics_audit): a
-- row-level BEFORE UPDATE OR DELETE trigger RAISES unconditionally, plus a statement-
-- level BEFORE TRUNCATE trigger. The REVOKE is kept as defence-in-depth for a future
-- non-owner app role.
--
-- FK: app.org_plan.org_id REFERENCES app.organizations(id) -- organizations table from
-- migration 035 (Epic 21). PK of app.organizations is 'id' (TEXT, prefixed ULID 'org_').
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/056_org_plan_entitlements.sql
--
-- Schema-Change-Checklist (CONTRIBUTING.md):
--   [x] Additive & idempotent (IF NOT EXISTS everywhere; whole file is replayable)
--   [x] New columns are NULL-able or have defaults
--   [x] No destructive DROP/ALTER on populated columns
-- Verified: last real migration is 055 (context_events_mmm_cols). This is 056.
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- ---------------------------------------------------------------------------
-- 1. app.org_plan -- current plan per org.
--
-- One row per org (org_id is the PK). Absent row => org is implicitly 'trial'
-- (the Python module derives the default without requiring a row to be inserted
-- for every new org). Set_org_plan upserts here + inserts one history row in
-- the SAME transaction.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.org_plan (
    org_id          TEXT        PRIMARY KEY
                    REFERENCES app.organizations(id) ON DELETE CASCADE,
    plan            TEXT        NOT NULL DEFAULT 'trial'
                    CHECK (plan IN ('trial', 'full', 'internal')),
    entitlements    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    granted_by      TEXT,                                      -- identity subject (AD-14)
    granted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_org_plan_plan ON app.org_plan (plan);

-- updated_at trigger (reuse app.set_updated_at() from migration 023).
DROP TRIGGER IF EXISTS trg_org_plan_updated_at ON app.org_plan;
CREATE TRIGGER trg_org_plan_updated_at
    BEFORE UPDATE ON app.org_plan
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- ---------------------------------------------------------------------------
-- 2. app.org_plan_history -- append-only audit trail (pattern 049).
--
-- Every change written by set_org_plan inserts one row here (same transaction).
-- The table is NEVER updated or deleted; triggers enforce this at the DB level
-- regardless of caller role (owner included).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.org_plan_history (
    id              TEXT        PRIMARY KEY,           -- prefixed ULID: 'oplh_'
    org_id          TEXT        NOT NULL
                    REFERENCES app.organizations(id) ON DELETE CASCADE,
    plan            TEXT        NOT NULL,              -- plan at time of grant
    entitlements    JSONB       NOT NULL,              -- entitlements at time of grant
    granted_by      TEXT,                              -- identity subject (AD-14)
    at              TIMESTAMPTZ NOT NULL DEFAULT now() -- timestamp of this history row
);

CREATE INDEX IF NOT EXISTS ix_org_plan_history_org ON app.org_plan_history (org_id);
CREATE INDEX IF NOT EXISTS ix_org_plan_history_at  ON app.org_plan_history (at DESC);

-- APPEND-ONLY (pattern 002/003/004): REVOKE wrapped so the migration is replayable
-- on throwaway test databases where the 'connector' role may NOT exist.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'connector') THEN
        REVOKE UPDATE, DELETE ON app.org_plan_history FROM connector;
    END IF;
END
$$;

-- Row-level guard: block UPDATE and DELETE regardless of caller role (owner included).
CREATE OR REPLACE FUNCTION app.org_plan_history_block_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'app.org_plan_history is append-only (Story 34.1): % blocked', TG_OP
        USING ERRCODE = 'raise_exception';
END;
$$;

DROP TRIGGER IF EXISTS org_plan_history_append_only ON app.org_plan_history;
CREATE TRIGGER org_plan_history_append_only
    BEFORE UPDATE OR DELETE ON app.org_plan_history
    FOR EACH ROW
    EXECUTE FUNCTION app.org_plan_history_block_mutation();

-- Statement-level guard: row-level triggers do not fire on TRUNCATE, and the owner
-- keeps the TRUNCATE privilege regardless of REVOKE. Close the last mutation path.
CREATE OR REPLACE FUNCTION app.org_plan_history_block_truncate()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'app.org_plan_history is append-only (Story 34.1): TRUNCATE blocked'
        USING ERRCODE = 'raise_exception';
END;
$$;

DROP TRIGGER IF EXISTS org_plan_history_block_truncate ON app.org_plan_history;
CREATE TRIGGER org_plan_history_block_truncate
    BEFORE TRUNCATE ON app.org_plan_history
    FOR EACH STATEMENT
    EXECUTE FUNCTION app.org_plan_history_block_truncate();

COMMENT ON TABLE app.org_plan IS
    'Story 34.1: current plan per org (trial|full|internal). Absent row = implicit trial. '
    'Set via set_org_plan (server/core/org_entitlements.py) which writes org_plan_history '
    'in the same transaction. Enforcement guards land in Stories 34.2/34.3.';

COMMENT ON TABLE app.org_plan_history IS
    'Story 34.1: append-only audit trail of every org_plan change. UPDATE/DELETE blocked '
    'by triggers (pattern 003/004 from migration 049 -- owner-proof). Each set_org_plan '
    'call inserts exactly one row here, in the same transaction as the org_plan upsert.';

COMMIT;
