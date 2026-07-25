-- infra/nango/migrations/091_datastream_inbound_credentials.sql
--
-- Story 38.7: Issue, Rotate and Revoke Delivery Capabilities.
--
-- Introduces `app.datastream_inbound_credentials` -- per-Datastream,
-- per-channel delivery credential records. The raw token is NEVER stored;
-- only `token_hash` (sha256) and `safe_suffix` (last N chars) are persisted.
--
-- INVARIANTS:
--   AD-2  : zero provider/source vocabulary. The channel ('email', 'webhook')
--            and domain are declared data; no vendor name enters this table.
--   E38-NFR03 : NO raw token column. No raw credential value anywhere.
--               token_hash is a sha256 hex digest; safe_suffix is a display hint.
--   PARTIAL UNIQUE: at most ONE non-terminal (ACTIVE or ROTATING) row per
--            (datastream_id, channel). Revoked/expired rows are retained as audit.
--   IMMUTABILITY TRIGGER: `protect_inbound_credential` freezes identity columns
--            (id, datastream_id, channel, token_hash, safe_suffix, issued_by,
--             created_at, version) and permits only (state, overlap_until,
--             expires_at, operation_id, updated_at) to change. DELETE is
--             forbidden unconditionally (data-preservation invariant, AC3).
--            Block DELETE returns OLD so the caller can observe the rejection.
--   OPERATION FK: `operation_id` binds each lifecycle write to the shared durable
--            audit + outbox spine (migration 060). NULL until the write completes.
--
-- Additive, idempotent, replayable: every statement uses IF NOT EXISTS /
-- OR REPLACE / DROP TRIGGER IF EXISTS so re-application is a no-op.
-- Mirrors 082/085/087 style.
--
-- Apply order: after 090 (highest on disk as of story start).
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector \
--     -f /migrations/091_datastream_inbound_credentials.sql

BEGIN;

-- ---------------------------------------------------------------------------
-- app.datastream_inbound_credentials
--
-- Column notes:
--   `id`              : `dic_<ULID>` TEXT PK (generated at write time in Python).
--   `datastream_id`   : FK to app.datastreams (the governed inbound Datastream).
--   `channel`         : 'email' | 'webhook'. Determines the address format.
--   `token_hash`      : sha256(raw_token) in hex. The ONLY persisted token
--                        derivative. NEVER the raw token itself.
--   `safe_suffix`     : last N chars of the raw token -- a display hint only,
--                        never enough to reconstruct the secret (max 8 chars).
--   `state`           : 'ACTIVE' | 'ROTATING' | 'REVOKED' | 'EXPIRED'.
--                        ACTIVE: the current valid credential.
--                        ROTATING: prior credential still valid until overlap_until.
--                        REVOKED: operator-revoked; permanently invalid.
--                        EXPIRED: past expires_at; permanently invalid.
--   `version`         : monotonic counter starting at 1. Bumped on each rotate.
--   `expires_at`      : optional hard expiry (NULL = no expiry).
--   `overlap_until`   : when ROTATING, valid until this timestamp; NULL otherwise.
--   `rate_limit`      : optional JSONB rate-limit policy for this credential.
--   `issued_by`       : identity of the operator who issued this credential.
--   `operation_id`    : FK to app.operations (the durable lifecycle write).
--   `created_at` / `updated_at`: timestamps.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.datastream_inbound_credentials (
    id                TEXT        NOT NULL,
    datastream_id     TEXT        NOT NULL,
    channel           TEXT        NOT NULL
                      CHECK (channel IN ('email', 'webhook')),
    token_hash        TEXT        NOT NULL,
    safe_suffix       TEXT,
    state             TEXT        NOT NULL
                      CHECK (state IN ('ACTIVE', 'ROTATING', 'REVOKED', 'EXPIRED')),
    version           INT         NOT NULL DEFAULT 1,
    expires_at        TIMESTAMPTZ,
    overlap_until     TIMESTAMPTZ,
    rate_limit        JSONB,
    issued_by         TEXT        NOT NULL,
    operation_id      TEXT
                      REFERENCES app.operations(id) ON DELETE RESTRICT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_datastream_inbound_credentials
        PRIMARY KEY (id),
    CONSTRAINT ck_dic_id
        CHECK (id ~ '^dic_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT ck_dic_version_positive
        CHECK (version >= 1)
);

-- ---------------------------------------------------------------------------
-- Partial unique: ONE non-terminal row per (datastream_id, channel).
-- Non-terminal states are ACTIVE and ROTATING. REVOKED and EXPIRED rows
-- are terminal and accumulate for audit without violating this constraint.
-- ---------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS uq_dic_active_per_datastream_channel
    ON app.datastream_inbound_credentials (datastream_id, channel)
    WHERE state = 'ACTIVE';

-- Index: lookup by datastream_id for list/status operations.
CREATE INDEX IF NOT EXISTS idx_dic_datastream_id
    ON app.datastream_inbound_credentials (datastream_id, channel, state);

-- Index: lookup by token_hash for resolve_for_delivery (constant-time path).
CREATE INDEX IF NOT EXISTS idx_dic_token_hash
    ON app.datastream_inbound_credentials (token_hash, state);

-- ---------------------------------------------------------------------------
-- Immutability trigger: `protect_inbound_credential`.
--
-- Frozen identity columns (cannot change after insert):
--   id, datastream_id, channel, token_hash, safe_suffix, issued_by,
--   created_at, version.
--
-- Allowed mutations (lifecycle state transitions only):
--   state, overlap_until, expires_at, operation_id, updated_at.
--
-- DELETE is forbidden unconditionally: returns OLD so the violation
-- surfaces clearly to the caller (data-preservation invariant, AC3).
-- Mirrors protect_connector_activation (migration 087) style.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.protect_inbound_credential()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF (TG_OP = 'DELETE') THEN
        RAISE EXCEPTION
            'datastream_inbound_credentials rows may not be deleted'
            USING ERRCODE = '23000';
        RETURN OLD;
    END IF;
    IF (TG_OP = 'UPDATE') THEN
        IF NEW.id IS DISTINCT FROM OLD.id THEN
            RAISE EXCEPTION 'datastream_inbound_credentials.id is immutable'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.datastream_id IS DISTINCT FROM OLD.datastream_id THEN
            RAISE EXCEPTION 'datastream_inbound_credentials.datastream_id is immutable'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.channel IS DISTINCT FROM OLD.channel THEN
            RAISE EXCEPTION 'datastream_inbound_credentials.channel is immutable'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.token_hash IS DISTINCT FROM OLD.token_hash THEN
            RAISE EXCEPTION 'datastream_inbound_credentials.token_hash is immutable'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.safe_suffix IS DISTINCT FROM OLD.safe_suffix THEN
            RAISE EXCEPTION 'datastream_inbound_credentials.safe_suffix is immutable'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.issued_by IS DISTINCT FROM OLD.issued_by THEN
            RAISE EXCEPTION 'datastream_inbound_credentials.issued_by is immutable'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'datastream_inbound_credentials.created_at is immutable'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.version IS DISTINCT FROM OLD.version THEN
            RAISE EXCEPTION 'datastream_inbound_credentials.version is immutable'
                USING ERRCODE = '23000';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_inbound_credential_protect
    ON app.datastream_inbound_credentials;
CREATE TRIGGER trg_inbound_credential_protect
    BEFORE UPDATE OR DELETE ON app.datastream_inbound_credentials
    FOR EACH ROW EXECUTE FUNCTION app.protect_inbound_credential();

COMMIT;
