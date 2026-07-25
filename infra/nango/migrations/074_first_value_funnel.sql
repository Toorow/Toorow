-- Story 36.19: First-value funnel analytics + separate Support-access ledger (AD-32 / E36-FR09).
-- Additive, idempotent, replayable. Zero provider/source vocabulary (AD-2).
--
-- WHY TWO SEPARATE TABLES (NOT app.audit_log, NOT app.operations)
-- ----------------------------------------------------------------
-- app.first_value_events is a PRODUCT-ANALYTICS sink, deliberately distinct from the
-- security audit_log (migration 060) and the durable-operation store. AD-32 forbids
-- joining behavioural product analytics with tenant-identifying security evidence:
--   1. Product analytics carries ONLY allowlisted enums (stage/outcome/recovery/
--      abandon reason), a PSEUDONYMOUS keyed-hash journey reference, and a COARSE
--      duration bucket. It NEVER stores raw org/email/project/provider identifiers,
--      raw errors, prompt text, or free-form reasons -- the CHECK constraints below
--      make every enum column reject anything off the allowlist AT THE DB LEVEL, so a
--      buggy or hostile caller cannot smuggle content past the domain validator.
--   2. The Support-access ledger (app.support_access_ledger) is the SEPARATE
--      purpose/expiry/audit surface for human investigation. It is NOT joined into any
--      product-analytics query: it lives in its own table with its own purpose/expiry
--      and links back to the durable security audit event by id only. Keeping it apart
--      is what prevents Support disclosures from re-identifying analytics cohorts.
--   3. Both tables are APPEND-ONLY for their evidence: first_value_events forbids
--      UPDATE and any DELETE that is not a retention purge (a purge sets a session GUC
--      the trigger checks), so behavioural history cannot be silently rewritten, yet
--      the versioned retention policy can still verifiably remove expired rows.
--
-- INVARIANT: NO raw org/email/project/provider column exists here BY CONSTRUCTION.
-- The only tenant linkage is a one-way keyed hash (HMAC-SHA256 with a server pepper),
-- so a stolen analytics dump cannot be reversed to a customer, and a query that would
-- difference a single tenant is suppressed in the read path (small-cohort suppression).

BEGIN;

-- ---------------------------------------------------------------------------
-- Product-analytics sink: allowlisted enums + pseudonymous ref + coarse bucket.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.first_value_events (
    id                  TEXT PRIMARY KEY,
    -- PSEUDONYMOUS journey reference: HMAC-SHA256(pepper, "journey:<org>:<project>:...")
    -- computed server-side. NEVER the raw org id, email, project id or provider account.
    -- Two events of the same journey share this hash so a funnel can be reconstructed
    -- WITHOUT knowing which tenant it belongs to.
    journey_ref_hash    TEXT NOT NULL CHECK (journey_ref_hash ~ '^[0-9a-f]{64}$'),
    -- Coarse cohort discriminator (also a keyed hash) used ONLY to bucket cross-tenant
    -- cohorts for suppression; never the raw org id.
    cohort_ref_hash     TEXT NOT NULL CHECK (cohort_ref_hash ~ '^[0-9a-f]{64}$'),
    -- The ~12 first-value funnel stages (E36-FR09). Closed set enforced in DB + domain.
    stage               TEXT NOT NULL CHECK (stage IN (
                            'invitation_delivery',
                            'invitation_acceptance',
                            'source_authorization',
                            'account_selection',
                            'preview',
                            'recent_pull',
                            'report_readiness',
                            'history_completion',
                            'host_connection',
                            'first_correct_answer',
                            'second_user_reproduction',
                            'recovery_action')),
    -- Closed outcome set.
    outcome             TEXT NOT NULL CHECK (outcome IN (
                            'started', 'succeeded', 'degraded', 'failed',
                            'abandoned', 'blocked')),
    -- COARSE duration bucket only -- an exact duration is NEVER stored. NULL when the
    -- stage carries no elapsed time.
    duration_bucket     TEXT CHECK (duration_bucket IS NULL OR duration_bucket IN (
                            'lt_1m', '1m_5m', '5m_30m', '30m_2h', '2h_1d', 'gt_1d')),
    -- Closed recovery-kind set (only meaningful for the recovery_action stage). NULL otherwise.
    recovery_kind       TEXT CHECK (recovery_kind IS NULL OR recovery_kind IN (
                            'reconnect', 'retry', 'refetch', 'reload', 'reprocess',
                            'replace', 'reconcile', 'rollback', 'support_handoff')),
    -- Closed abandon-reason set -- NO free text. NULL unless outcome = 'abandoned'.
    abandon_reason      TEXT CHECK (abandon_reason IS NULL OR abandon_reason IN (
                            'awaiting_external_owner', 'authorization_expired',
                            'no_compatible_host', 'readiness_never_reached',
                            'reproduction_failed', 'unknown')),
    -- Versioned retention/policy stamps so a purge is deterministic and verifiable.
    policy_version      TEXT NOT NULL,
    occurred_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    retention_expires_at TIMESTAMPTZ NOT NULL,
    -- A recovery_kind is only valid on the recovery stage; an abandon_reason only when
    -- abandoned. Keeps the analytics semantically honest at the DB level.
    CONSTRAINT ck_first_value_recovery_scope
        CHECK (recovery_kind IS NULL OR stage = 'recovery_action'),
    CONSTRAINT ck_first_value_abandon_scope
        CHECK (abandon_reason IS NULL OR outcome = 'abandoned')
);

CREATE INDEX IF NOT EXISTS first_value_events_journey
    ON app.first_value_events (journey_ref_hash, occurred_at);
CREATE INDEX IF NOT EXISTS first_value_events_cohort
    ON app.first_value_events (cohort_ref_hash, stage, outcome);
CREATE INDEX IF NOT EXISTS first_value_events_retention
    ON app.first_value_events (retention_expires_at);

-- Append-only for behavioural history. UPDATE is always forbidden. DELETE is forbidden
-- UNLESS the retention purge sets `app.funnel_purge = 'on'` for the transaction, so the
-- versioned retention policy can verifiably remove expired rows while ad-hoc deletes stay
-- blocked (no silent rewriting of the funnel).
CREATE OR REPLACE FUNCTION app.protect_first_value_events()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'first_value_events is append-only (no UPDATE)';
    END IF;
    -- DELETE: allow only inside a retention purge that flagged itself.
    IF current_setting('app.funnel_purge', true) IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION 'first_value_events rows are removable only by the retention purge';
    END IF;
    RETURN OLD;
END;
$$;

DROP TRIGGER IF EXISTS trg_first_value_events_append_only ON app.first_value_events;
CREATE TRIGGER trg_first_value_events_append_only
    BEFORE UPDATE OR DELETE ON app.first_value_events
    FOR EACH ROW EXECUTE FUNCTION app.protect_first_value_events();

-- ---------------------------------------------------------------------------
-- Tenant-facing linkage: which project a pseudonymous journey belongs to.
-- ---------------------------------------------------------------------------
-- This side table maps a journey_ref_hash to its owning project so the TENANT read
-- (`tenant_journeys`) can show an authorized owner ONLY their own project's journeys,
-- resolved through the strict access seam. It deliberately lives OUTSIDE
-- app.first_value_events: the analytics sink itself stays project-anonymous (only the
-- keyed hash), while this bounded, access-checked bridge lets a tenant see their journeys.
-- It is NEVER used by the cross-tenant cohort read.
CREATE TABLE IF NOT EXISTS app.first_value_project_journeys (
    journey_ref_hash    TEXT NOT NULL CHECK (journey_ref_hash ~ '^[0-9a-f]{64}$'),
    project_id          TEXT NOT NULL REFERENCES app.projects(id) ON DELETE CASCADE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (journey_ref_hash, project_id)
);

CREATE INDEX IF NOT EXISTS first_value_project_journeys_project
    ON app.first_value_project_journeys (project_id);

-- ---------------------------------------------------------------------------
-- SEPARATE Support-access ledger: distinct purpose / expiry / audit linkage.
-- NOT joined into any product-analytics query. Only pseudonymous hashes here.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.support_access_ledger (
    id              TEXT PRIMARY KEY,
    -- Why this Support access was granted (closed set at the domain layer).
    purpose         TEXT NOT NULL,
    -- Keyed hash of the human Support actor -- never the raw email/identity.
    actor_hash      TEXT NOT NULL CHECK (actor_hash ~ '^[0-9a-f]{64}$'),
    -- Keyed hash of the scope the access covers (never the raw org/project id).
    scope_ref_hash  TEXT NOT NULL CHECK (scope_ref_hash ~ '^[0-9a-f]{64}$'),
    -- Short-lived where applicable: when the access grant lapses.
    expires_at      TIMESTAMPTZ NOT NULL,
    -- Link back to the durable security audit event (migration 060) by id only. This is
    -- the ONE bridge to the audit trail; product analytics never follows it.
    audit_event_id  TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS support_access_ledger_actor
    ON app.support_access_ledger (actor_hash, created_at DESC);
CREATE INDEX IF NOT EXISTS support_access_ledger_expiry
    ON app.support_access_ledger (expires_at);

-- Append-only: a Support-access grant is durable evidence, never mutated/deleted.
CREATE OR REPLACE FUNCTION app.protect_support_access_ledger()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'support_access_ledger is append-only';
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_support_access_ledger_append_only ON app.support_access_ledger;
CREATE TRIGGER trg_support_access_ledger_append_only
    BEFORE UPDATE OR DELETE ON app.support_access_ledger
    FOR EACH ROW EXECUTE FUNCTION app.protect_support_access_ledger();

COMMIT;
