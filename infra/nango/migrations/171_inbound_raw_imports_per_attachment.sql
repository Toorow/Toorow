-- 171_inbound_raw_imports_per_attachment.sql
--
-- Story 38.9 AC1: ONE immutable raw import PER ATTACHMENT, with independent
-- outcomes. Until this migration the unit of inbound evidence was the DELIVERY,
-- not the FILE -- and that is a different thing.
--
-- WHAT WAS ACTUALLY THERE, MEASURED BEFORE WRITING THIS:
--
--     grep -rl raw_import infra/nango/migrations/     -- empty, no such table
--
--   `app.inbound_receipts` (migration 094) carries ONE `quarantine_uri` and a
--   scalar `attachment_count`. So a delivery bearing three files had one row,
--   one URI, one state. And `core/inbound_processing.py` matched the schema it
--   was given: it processed the FIRST data attachment and returned the rest
--   under `ignored_attachments`, a shortcut its own docstring declared openly.
--
--   The consequence is not cosmetic. AC4 of the same story asks for a
--   per-attachment deterministic outcome, AC1 of 38.10 asks for a scan verdict
--   per file, 38.14 asks for an inbox row per attachment, and 38.18 asks to
--   reprocess a retained raw file by name. None of those can be expressed
--   against a table whose grain is the delivery. This is the missing grain, and
--   three stories were waiting on it.
--
-- WHAT THIS TABLE IS, AND IS NOT:
--
--   It IS the durable per-file evidence: which bytes arrived, under which
--   delivery, in which order, with which content hash and which terminal
--   outcome. It is append-only in the same sense as the receipt: history is not
--   rewritable.
--
--   It is NOT a second import ledger. `app.managed_feed_import_ledger` remains
--   the one ledger of imports, and `import_ledger_id` here is a pointer INTO
--   it, exactly as `inbound_receipts.import_ledger_id` already is. A raw import
--   that lands produces a ledger row through the SAME `run_import` the direct
--   upload uses -- E38-FR10, "delivery channel never creates a second mapping
--   or publication engine".
--
-- INVARIANTS CARRIED OVER FROM 094 DELIBERATELY:
--
--   AD-2       : no provider/source vocabulary. `channel` is not repeated here;
--                it belongs to the receipt, and duplicating it would let the two
--                rows disagree.
--   E38-NFR03  : no token, no signature, no raw recipient address, no row
--                sample. `filename` is stored because an operator has to
--                recognise their own file, and it is UNTRUSTED DISPLAY DATA
--                ONLY -- the storage path is derived from `content_hash`
--                (E38-FR09: "generated content-addressed names"), never from
--                the filename. The CHECK on `content_hash` is what makes that
--                statement enforceable rather than merely intended.
--   IMMUTABILITY: identity columns freeze at insert; only the lifecycle columns
--                move. DELETE is refused EXCEPT inside an audited tenant erasure.
--
-- THE RGPD HATCH IS PRESENT AT BIRTH, AND THAT IS THE POINT.
--   Migration 097 gave `app.file_source_templates` an append-only trigger with
--   no escape, and migration 169 -- seventy-two migrations later -- had to add
--   the hatch after a test fixture became permanently undeletable in production.
--   The class lesson was written down there: every append-only table inside the
--   org tree must be reachable by an audited erasure, or an RGPD request cannot
--   be honoured. `app.inbound_receipts` got its hatch retrofitted by 099. This
--   table is born with it: DELETE is allowed only when the transaction has
--   flagged itself with `SET LOCAL app.rgpd_erasure = 'on'`, which is what
--   `core/org_purge.py` does and nothing else does.
--
--   `org_purge.plan_purge` reads the FK graph from `pg_constraint` at call time
--   (org_purge.py:17), so this table joins the tenant tree automatically through
--   its FK to `app.inbound_receipts` -- no purge list to update by hand. That is
--   also why `receipt_id` is NOT NULL: a nullable FK column would silently drop
--   the row out of the traversal (org_purge.py:199).
--
-- Additive, idempotent, replayable: IF NOT EXISTS / OR REPLACE / DROP TRIGGER
-- IF EXISTS throughout. Mirrors 094 style.
--
-- Apply order: after 170.

BEGIN;

-- ---------------------------------------------------------------------------
-- app.inbound_raw_imports
--
-- Column notes:
--   `id`              : `inbraw_<ULID>` TEXT PK (generated at write time).
--   `receipt_id`      : FK to app.inbound_receipts. NOT NULL -- see the purge
--                        note above; also, a raw import with no delivery has no
--                        provenance and must not be representable.
--   `datastream_id`   : denormalised from the receipt so the common read path
--                        (list a Datastream's attachments) needs no join, and so
--                        a row can never be attributed to another tenant by a
--                        later UPDATE (the immutability trigger freezes it).
--   `ordinal`         : 0-based position of the attachment WITHIN its delivery.
--                        UNIQUE per (receipt_id, ordinal): re-running the
--                        expansion of one delivery cannot create a second row
--                        for the same attachment. This is what makes the worker
--                        idempotent at the FILE grain, mirroring what
--                        (datastream_id, provider_event_id) does at the DELIVERY
--                        grain in 094.
--   `filename`        : the delivered filename, UNTRUSTED. Display only. Never a
--                        path component, never an authentication input.
--   `media_type_declared` : the content type the sender claimed. Evidence, not
--                        a decision -- E38-NFR02 forbids trusting a MIME claim.
--   `media_type_detected` : what independent content detection concluded. NULL
--                        until 38.10 runs the detection; the two columns exist
--                        separately precisely so a mismatch is representable
--                        rather than resolved by overwriting one with the other.
--   `size_bytes`      : bytes actually stored.
--   `content_hash`    : sha256 hex of the stored bytes. CHECK: exactly 64 hex
--                        chars. This is the content-addressed identity, and the
--                        basis of the storage path.
--   `quarantine_uri`  : opaque pointer to the stored bytes.
--   `state`           : lifecycle of THIS file, independent of its siblings:
--                        'RECEIVED'  -- bytes are durably stored, nothing else ran
--                        'SCANNING'  -- 38.10 controls are running
--                        'ACCEPTED'  -- controls passed, may reach a parser
--                        'REJECTED'  -- a control refused it; terminal
--                        'LANDED'    -- it produced an import ledger row
--                        'FAILED'    -- processing failed; terminal
--   `scan_verdict`    : redacted, operator-facing scan evidence (JSONB). 38.10
--                        fills it; it exists here so the verdict cannot outlive
--                        or drift from the file it judged.
--   `error_code`      : short stable classifier, set on REJECTED/FAILED.
--   `error_detail`    : operator-facing detail. Never a raw row sample.
--   `import_ledger_id`: the mfl_<ULID> this file produced, when it landed.
--   `retention_expires_at` : after this instant the bytes may be lifecycle-
--                        deleted and reprocessing becomes unavailable with an
--                        explicit reason (E38-NFR15). NULL = no policy set yet.
--   `legal_hold`      : when true, retention enforcement must not delete the
--                        object. A hold is a decision, so it is a column, not an
--                        absence.
--   `operation_id`    : FK to app.operations -- the durable audit + outbox spine.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.inbound_raw_imports (
    id                  TEXT        NOT NULL,
    receipt_id          TEXT        NOT NULL
                        REFERENCES app.inbound_receipts(id) ON DELETE RESTRICT,
    datastream_id       TEXT        NOT NULL
                        REFERENCES app.datastreams(id) ON DELETE RESTRICT,
    ordinal             INT         NOT NULL,
    filename            TEXT,
    media_type_declared TEXT,
    media_type_detected TEXT,
    size_bytes          BIGINT      NOT NULL,
    content_hash        TEXT        NOT NULL
                        CHECK (
                            LENGTH(content_hash) = 64
                            AND content_hash ~ '^[0-9a-f]{64}$'
                        ),
    quarantine_uri      TEXT,
    state               TEXT        NOT NULL DEFAULT 'RECEIVED'
                        CHECK (state IN (
                            'RECEIVED', 'SCANNING', 'ACCEPTED',
                            'REJECTED', 'LANDED', 'FAILED'
                        )),
    scan_verdict        JSONB,
    error_code          TEXT,
    error_detail        TEXT,
    import_ledger_id    TEXT,
    retention_expires_at TIMESTAMPTZ,
    legal_hold          BOOLEAN     NOT NULL DEFAULT FALSE,
    operation_id        TEXT
                        REFERENCES app.operations(id) ON DELETE RESTRICT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_inbound_raw_imports
        PRIMARY KEY (id),
    CONSTRAINT ck_inbraw_id
        CHECK (id ~ '^inbraw_[0-9A-Za-z]{26,}$'),
    CONSTRAINT ck_inbraw_ordinal_nonneg
        CHECK (ordinal >= 0),
    CONSTRAINT ck_inbraw_size_nonneg
        CHECK (size_bytes >= 0)
);

-- ---------------------------------------------------------------------------
-- Idempotent expansion at the FILE grain: at most ONE row per attachment of a
-- delivery. A redelivered or re-expanded manifest reconciles to the existing
-- rows instead of duplicating them.
-- ---------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS uq_inbraw_receipt_ordinal
    ON app.inbound_raw_imports (receipt_id, ordinal);

-- Common read path: a Datastream's attachment inbox, newest first (38.14).
CREATE INDEX IF NOT EXISTS idx_inbraw_datastream_created
    ON app.inbound_raw_imports (datastream_id, created_at DESC);

-- Duplicate-content lookup (38.9 AC4). Deliberately NOT unique: whether the
-- same bytes arriving twice is a duplicate to skip or a legitimate re-send is a
-- DATASTREAM POLICY question, and a unique constraint would answer it here, for
-- every tenant, forever. The index makes the policy cheap to evaluate; it does
-- not make the decision.
CREATE INDEX IF NOT EXISTS idx_inbraw_datastream_content_hash
    ON app.inbound_raw_imports (datastream_id, content_hash);

-- Retention sweep: find expired objects that are not on legal hold.
CREATE INDEX IF NOT EXISTS idx_inbraw_retention
    ON app.inbound_raw_imports (retention_expires_at)
    WHERE legal_hold = FALSE;

-- ---------------------------------------------------------------------------
-- Immutability trigger: `protect_inbound_raw_import`.
--
-- Frozen identity columns: id, receipt_id, datastream_id, ordinal, filename,
--   media_type_declared, size_bytes, content_hash, created_at.
--
--   `filename` and `media_type_declared` are frozen ON PURPOSE. They are the
--   sender's claim, and the record of what was claimed is evidence: letting a
--   later write correct them would erase the mismatch that 38.10 exists to
--   detect. `media_type_detected` is separate and mutable for exactly that
--   reason.
--
-- Allowed mutations: media_type_detected, quarantine_uri, state, scan_verdict,
--   error_code, error_detail, import_ledger_id, retention_expires_at,
--   legal_hold, operation_id, updated_at.
--
-- DELETE: refused, EXCEPT inside an audited tenant erasure that has set
--   `app.rgpd_erasure = 'on'` transaction-locally. See the header note.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.protect_inbound_raw_import()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF (TG_OP = 'DELETE') THEN
        IF current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on' THEN
            RAISE EXCEPTION
                'inbound_raw_imports rows may not be deleted'
                USING ERRCODE = '23000';
        END IF;
        RETURN OLD;
    END IF;
    IF (TG_OP = 'UPDATE') THEN
        IF NEW.id IS DISTINCT FROM OLD.id THEN
            RAISE EXCEPTION 'inbound_raw_imports.id is immutable'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.receipt_id IS DISTINCT FROM OLD.receipt_id THEN
            RAISE EXCEPTION 'inbound_raw_imports.receipt_id is immutable'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.datastream_id IS DISTINCT FROM OLD.datastream_id THEN
            RAISE EXCEPTION 'inbound_raw_imports.datastream_id is immutable'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.ordinal IS DISTINCT FROM OLD.ordinal THEN
            RAISE EXCEPTION 'inbound_raw_imports.ordinal is immutable'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.filename IS DISTINCT FROM OLD.filename THEN
            RAISE EXCEPTION 'inbound_raw_imports.filename is immutable'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.media_type_declared IS DISTINCT FROM OLD.media_type_declared THEN
            RAISE EXCEPTION
                'inbound_raw_imports.media_type_declared is immutable'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.size_bytes IS DISTINCT FROM OLD.size_bytes THEN
            RAISE EXCEPTION 'inbound_raw_imports.size_bytes is immutable'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.content_hash IS DISTINCT FROM OLD.content_hash THEN
            RAISE EXCEPTION 'inbound_raw_imports.content_hash is immutable'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'inbound_raw_imports.created_at is immutable'
                USING ERRCODE = '23000';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_inbound_raw_import_protect
    ON app.inbound_raw_imports;
CREATE TRIGGER trg_inbound_raw_import_protect
    BEFORE UPDATE OR DELETE ON app.inbound_raw_imports
    FOR EACH ROW EXECUTE FUNCTION app.protect_inbound_raw_import();

COMMIT;
