-- Story 36.13: Operations-profile bounded recovery preparations (AD-27).
-- Additive and replayable. Zero provider/source vocabulary (AD-2).
--
-- A write-effect recovery (retry / bounded refetch / execution-state reconcile)
-- follows prepare-confirm-commit: `prepare_datastream_recovery` records ONE
-- immutable proposal here; a trusted `confirm_datastream_recovery` then routes
-- exactly ONE durable operation through Story 36.2 (app.operations) and links it
-- back via `operation_id`. The proposal is the persisted, immutable evidence of
-- the target/versions/interval/impact/quota/lock/rollback/idempotency that was
-- shown to the confirmer -- it CANNOT ride app.operations.request_payload because
-- the operation does not exist until confirmation time, and the proposal needs
-- its own prepared->confirmed/expired lifecycle plus an in-place immutability
-- guard (mirrors migration 067's approach for capability contexts).
--
-- This table stores NO mapping-publication or live-pointer authority: kind is
-- constrained to the three Operations recovery verbs only (retry, refetch,
-- reconcile). Publication/reconciliation of a live pointer belongs to Governance
-- (Story 36.18) and is intentionally NOT expressible here.

BEGIN;

CREATE TABLE IF NOT EXISTS app.operation_preparations (
    id                      TEXT PRIMARY KEY,
    -- Target scope. project_id + datastream_id together bound the recovery to one
    -- authorized resource path; org_id pins the effective organization used for the
    -- durable operation's idempotency namespace (app.operations).
    org_id                  TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    project_id              TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    datastream_id           TEXT NOT NULL REFERENCES app.datastreams(id) ON DELETE RESTRICT,
    -- The Operations recovery verb. Publication/live-pointer authority is EXCLUDED
    -- from this profile, so only these three write-effect verbs are representable.
    kind                    TEXT NOT NULL CHECK (kind IN ('retry', 'refetch', 'reconcile')),
    actor                   TEXT NOT NULL,
    -- The immutable plan/mapping/policy/catalog/tool versions the proposal was
    -- pinned to. Re-checked at confirm time: a version drift refuses dispatch.
    target_versions         JSONB NOT NULL CHECK (jsonb_typeof(target_versions) = 'object'),
    -- The bounded {from, to} interval (refetch) or the target execution reference
    -- (reconcile). NULL for a whole-datastream retry with no explicit interval.
    interval                JSONB CHECK (interval IS NULL OR jsonb_typeof(interval) = 'object'),
    -- Human-visible expected impact (affected phase/rows/publication statement).
    impact                  JSONB NOT NULL DEFAULT '{}'::jsonb
                            CHECK (jsonb_typeof(impact) = 'object'),
    -- Quota / cost estimate shown in the proposal (platform, estimated points,
    -- pre_check verdict). Re-checked at confirm time: a violation refuses dispatch.
    quota                   JSONB NOT NULL DEFAULT '{}'::jsonb
                            CHECK (jsonb_typeof(quota) = 'object'),
    -- The concurrency lock the dispatch will contend for (an active-execution or
    -- publishing lock reference). A lock conflict at confirm time refuses dispatch.
    lock_ref                TEXT,
    -- The rollback path the operation exposes (e.g. the last-known-good pointer).
    rollback_ref            TEXT,
    -- One-way digest of the durable operation idempotency key (never the raw key):
    -- confirm reuses it so a duplicate confirm/timeout replays the SAME operation.
    idempotency_key_hash    TEXT NOT NULL CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    expires_at              TIMESTAMPTZ NOT NULL,
    -- Proposal lifecycle. `prepared` is the only state a confirm may consume; a
    -- confirmed proposal carries the linked durable operation_id.
    state                   TEXT NOT NULL DEFAULT 'prepared'
                            CHECK (state IN ('prepared', 'confirmed', 'expired')),
    operation_id            TEXT REFERENCES app.operations(id) ON DELETE RESTRICT,
    immutable               BOOLEAN NOT NULL DEFAULT TRUE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS operation_preparations_datastream
    ON app.operation_preparations (datastream_id, created_at DESC);

CREATE INDEX IF NOT EXISTS operation_preparations_prepared
    ON app.operation_preparations (org_id, state)
    WHERE state = 'prepared';

-- The prepared proposal is immutable (AD-27): confirmation may ONLY transition the
-- lifecycle (state prepared->confirmed/expired) and attach the durable operation_id.
-- Every substantive field (target, versions, interval, impact, quota, lock,
-- rollback, idempotency) is frozen in place -- a correction creates a NEW proposal.
CREATE OR REPLACE FUNCTION app.protect_operation_preparation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.immutable
       AND (
           NEW.org_id IS DISTINCT FROM OLD.org_id
           OR NEW.project_id IS DISTINCT FROM OLD.project_id
           OR NEW.datastream_id IS DISTINCT FROM OLD.datastream_id
           OR NEW.kind IS DISTINCT FROM OLD.kind
           OR NEW.actor IS DISTINCT FROM OLD.actor
           OR NEW.target_versions IS DISTINCT FROM OLD.target_versions
           OR NEW.interval IS DISTINCT FROM OLD.interval
           OR NEW.impact IS DISTINCT FROM OLD.impact
           OR NEW.quota IS DISTINCT FROM OLD.quota
           OR NEW.lock_ref IS DISTINCT FROM OLD.lock_ref
           OR NEW.rollback_ref IS DISTINCT FROM OLD.rollback_ref
           OR NEW.idempotency_key_hash IS DISTINCT FROM OLD.idempotency_key_hash
       ) THEN
        RAISE EXCEPTION 'operation preparation proposal is immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_operation_preparation_immutable ON app.operation_preparations;
CREATE TRIGGER trg_operation_preparation_immutable
    BEFORE UPDATE ON app.operation_preparations
    FOR EACH ROW EXECUTE FUNCTION app.protect_operation_preparation();

COMMIT;
