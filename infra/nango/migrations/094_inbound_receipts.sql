-- infra/nango/migrations/094_inbound_receipts.sql
--
-- Story 38.8: Durable Inbound-Receipt Ledger.
--
-- Introduces `app.inbound_receipts` -- an immutable, append-only record of each
-- verified inbound delivery (email/file) that has been resolved to a Datastream,
-- so deliveries are durably accounted for and de-duplicated on the provider's
-- replay key.
--
-- INVARIANTS:
--   AD-2  : zero provider/source vocabulary. The channel ('email', 'webhook')
--            is declared data; no vendor name enters this table.
--   E38-NFR03 : NO raw token or raw recipient address stored anywhere.
--               `recipient_hash` is a sha256 hex digest of the recipient address;
--               the raw `ds_<token>@<domain>` string is NEVER persisted.
--   UNIQUE DE-DUP: at most ONE row per (datastream_id, provider_event_id).
--            A replayed delivery does not create a second row; the caller
--            reconciles to the existing row (ON CONFLICT DO NOTHING path).
--   IMMUTABILITY TRIGGER: `protect_inbound_receipt` freezes identity columns
--            (id, datastream_id, credential_id, channel, provider_event_id,
--             recipient_hash, created_at) and permits only (state, quarantine_uri,
--             import_ledger_id, error_code, error_detail, operation_id,
--             updated_at) to change. DELETE is forbidden unconditionally
--            (data-preservation invariant, append-only contract).
--            Block DELETE returns OLD so the caller can observe the rejection.
--   OPERATION FK: `operation_id` binds each lifecycle write to the shared durable
--            audit + outbox spine (migration 060). NULL until the write completes.
--
-- Additive, idempotent, replayable: every statement uses IF NOT EXISTS /
-- OR REPLACE / DROP TRIGGER IF EXISTS so re-application is a no-op.
-- Mirrors 091 style.
--
-- Apply order: after 093 (highest on disk as of story start).
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector \
--     -f /migrations/094_inbound_receipts.sql

BEGIN;

-- ---------------------------------------------------------------------------
-- app.inbound_receipts
--
-- Column notes:
--   `id`               : `inbrx_<ULID>` TEXT PK (generated at write time in Python).
--   `datastream_id`    : FK to app.datastreams (the resolved target Datastream).
--   `credential_id`    : FK to app.datastream_inbound_credentials (nullable;
--                         the credential row that resolved this delivery).
--   `channel`          : 'email' | 'webhook'. Opaque declared data (AD-2).
--   `provider_event_id`: the provider replay/idempotency key. UNIQUE per
--                         (datastream_id, provider_event_id) to prevent
--                         double-recording on redelivery.
--   `recipient_hash`   : sha256(raw recipient address) in hex. The raw
--                         `ds_<token>@<domain>` string is NEVER stored here
--                         (E38-NFR03). CHECK: exactly 64 hex chars or NULL.
--   `attachment_count` : count of attachments reported by the provider.
--   `total_bytes`      : declared total byte size of all attachments (nullable).
--   `quarantine_uri`   : opaque pointer to stored bytes (nullable). Content of
--                         this URI is outside this table's contract.
--   `state`            : 'RECEIVED' | 'PROCESSING' | 'LANDED' | 'REJECTED' |
--                         'FAILED'. Default 'RECEIVED' at insert time.
--   `import_ledger_id` : the mfl_<ULID> produced when the delivery was landed
--                         (nullable; set on transition to LANDED).
--   `error_code`       : short error classifier (nullable; set on FAILED/REJECTED).
--   `error_detail`     : human-readable detail (nullable; operator-facing only).
--   `operation_id`     : FK to app.operations (the durable lifecycle write).
--   `created_at` / `updated_at`: timestamps.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.inbound_receipts (
    id                  TEXT        NOT NULL,
    datastream_id       TEXT        NOT NULL
                        REFERENCES app.datastreams(id) ON DELETE RESTRICT,
    credential_id       TEXT
                        REFERENCES app.datastream_inbound_credentials(id)
                        ON DELETE RESTRICT,
    channel             TEXT        NOT NULL
                        CHECK (channel IN ('email', 'webhook')),
    provider_event_id   TEXT        NOT NULL,
    recipient_hash      TEXT
                        CHECK (
                            recipient_hash IS NULL
                            OR (LENGTH(recipient_hash) = 64
                                AND recipient_hash ~ '^[0-9a-f]{64}$')
                        ),
    attachment_count    INT         NOT NULL DEFAULT 0,
    total_bytes         BIGINT,
    quarantine_uri      TEXT,
    state               TEXT        NOT NULL DEFAULT 'RECEIVED'
                        CHECK (state IN (
                            'RECEIVED', 'PROCESSING', 'LANDED',
                            'REJECTED', 'FAILED'
                        )),
    import_ledger_id    TEXT,
    error_code          TEXT,
    error_detail        TEXT,
    operation_id        TEXT
                        REFERENCES app.operations(id) ON DELETE RESTRICT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_inbound_receipts
        PRIMARY KEY (id),
    CONSTRAINT ck_inbrx_id
        CHECK (id ~ '^inbrx_[0-9A-Za-z]{26,}$'),
    CONSTRAINT ck_inbrx_attachment_count_nonneg
        CHECK (attachment_count >= 0),
    CONSTRAINT ck_inbrx_total_bytes_nonneg
        CHECK (total_bytes IS NULL OR total_bytes >= 0)
);

-- ---------------------------------------------------------------------------
-- Unique de-duplication: ONE row per (datastream_id, provider_event_id).
-- A replayed delivery is reconciled to the existing row without inserting a
-- duplicate (append-only + idempotent redelivery).
-- ---------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS uq_inbrx_datastream_provider_event
    ON app.inbound_receipts (datastream_id, provider_event_id);

-- Index: list by datastream newest-first (most common read path).
CREATE INDEX IF NOT EXISTS idx_inbrx_datastream_created
    ON app.inbound_receipts (datastream_id, created_at DESC);

-- Index: state-based queue scans (PROCESSING sweep, FAILED retry, etc.).
CREATE INDEX IF NOT EXISTS idx_inbrx_state
    ON app.inbound_receipts (state);

-- ---------------------------------------------------------------------------
-- Immutability trigger: `protect_inbound_receipt`.
--
-- Frozen identity columns (cannot change after insert):
--   id, datastream_id, credential_id, channel, provider_event_id,
--   recipient_hash, created_at.
--
-- Allowed mutations (lifecycle state transitions only):
--   state, quarantine_uri, import_ledger_id, error_code, error_detail,
--   operation_id, updated_at.
--
-- DELETE is forbidden unconditionally: returns OLD so the violation surfaces
-- clearly to the caller (append-only invariant, data-preservation contract).
-- Mirrors protect_inbound_credential (migration 091) style.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.protect_inbound_receipt()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF (TG_OP = 'DELETE') THEN
        RAISE EXCEPTION
            'inbound_receipts rows may not be deleted'
            USING ERRCODE = '23000';
        RETURN OLD;
    END IF;
    IF (TG_OP = 'UPDATE') THEN
        IF NEW.id IS DISTINCT FROM OLD.id THEN
            RAISE EXCEPTION 'inbound_receipts.id is immutable'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.datastream_id IS DISTINCT FROM OLD.datastream_id THEN
            RAISE EXCEPTION 'inbound_receipts.datastream_id is immutable'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.credential_id IS DISTINCT FROM OLD.credential_id THEN
            RAISE EXCEPTION 'inbound_receipts.credential_id is immutable'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.channel IS DISTINCT FROM OLD.channel THEN
            RAISE EXCEPTION 'inbound_receipts.channel is immutable'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.provider_event_id IS DISTINCT FROM OLD.provider_event_id THEN
            RAISE EXCEPTION 'inbound_receipts.provider_event_id is immutable'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.recipient_hash IS DISTINCT FROM OLD.recipient_hash THEN
            RAISE EXCEPTION 'inbound_receipts.recipient_hash is immutable'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'inbound_receipts.created_at is immutable'
                USING ERRCODE = '23000';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_inbound_receipt_protect
    ON app.inbound_receipts;
CREATE TRIGGER trg_inbound_receipt_protect
    BEFORE UPDATE OR DELETE ON app.inbound_receipts
    FOR EACH ROW EXECUTE FUNCTION app.protect_inbound_receipt();

COMMIT;
