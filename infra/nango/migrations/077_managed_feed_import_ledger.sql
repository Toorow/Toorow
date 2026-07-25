-- infra/nango/migrations/077_managed_feed_import_ledger.sql
--
-- Story 12.8: Write managed feeds through an immutable import ledger.
--
-- This is the KEYSTONE substrate for Stories 12.9 (CSV/Excel) and 12.10 (Google
-- Sheets sync). It introduces the managed-feed WRITER's durable state:
--
--   * app.managed_feed_import_ledger  -- the IMMUTABLE, append-only ledger. One
--       row per import ATTEMPT, minted BEFORE any raw row is written, carrying the
--       idempotency key hash + content hash + source metadata + plan/mapping
--       versions + the isolated candidate execution it created. A retry of the
--       SAME key with the SAME payload returns the existing row; a DIFFERENT
--       payload for the same key is REJECTED (deterministic conflict) by the
--       partial-unique idempotency index below + the app layer. Immutable via a
--       BEFORE UPDATE OR DELETE trigger EXCEPT the lifecycle terminal fields
--       (outcome / row-counts / landing coordinates), mirroring the frozen-review
--       pattern of migrations 070/071/073.
--
--   * app.managed_feed_rejected_rows   -- per-row rejection storage traceable to
--       field + rule + row_number + execution (Story 12.8 AC4). This is a
--       DEDICATED, project-scoped landing-adjacent table owned only by toorow (it
--       is NOT a mart; dbt is never its writer -- AD-8). It reuses the SEMANTICS of
--       migration 026 (pull_verifications.rejected_rows is a COUNT) but adds the
--       row-level traceability the managed-feed download contract needs. The
--       aggregate count still lives on the ledger row (rejected_row_count) so a
--       blocking threshold decision reads one row, not a scan.
--
-- WHY A NEW TABLE (not app.datastream_executions, not pull_verifications):
--   The candidate registry (042) already gives each import an execution row + the
--   atomic pointer + the append-only PUBLICATION log. It does NOT record the
--   pre-write import INTENT (idempotency key + content hash + source snapshot
--   metadata) that must exist BEFORE the first row is written so a crash-retry is
--   deterministic. That is this ledger's job. The ledger points AT the execution
--   (FK), it does not replace it: publication still flows through 042/12.5.
--
-- Invariants held:
--   AD-2  : zero provider/source vocabulary -- managed-feed logic is generic;
--           the format (csv/excel/google_sheets) is an opaque enum, no branch.
--   AD-8  : dbt is the ONLY writer of analytical marts. This ledger + the rejected
--           rows table are Postgres metadata; the RAW landing is a project-scoped
--           org_<wslug>_raw table written via open_raw_writer, NEVER a mart.
--   AD-9  : row counts / hashes are honest NULL until measured, never coerced to 0.
--   NFR14 : the ledger row + isolated candidate exist BEFORE any row is written
--           (atomic-safe: a crash leaves an unpublished candidate, prior data intact).
--   NFR15 : the ledger is append-only; a re-import is a NEW ledger row, never a
--           mutation; an unchanged snapshot is a no-op ledger row, not a duplicate.
--   NFR20 : every import attempt is a durable, queryable, immutable audit record.
--
-- Additive and idempotent after migration 042 (the candidate registry) and 026
-- (rejected_rows count). Every statement is IF NOT EXISTS / OR REPLACE /
-- pg_constraint-guarded so re-application is a no-op (matches 042/070/073 style).
--
-- Apply with (human-gated on Jean's authorization -- Supabase only):
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/077_managed_feed_import_ledger.sql

BEGIN;

-- ---------------------------------------------------------------------------
-- app.managed_feed_import_ledger -- the immutable import ledger.
--
-- One row per import ATTEMPT. Minted in create_import (state 'opened') BEFORE any
-- raw row lands. It binds:
--   * idempotency_key_hash  -- sha256 of the caller's key; UNIQUE per datastream
--                              (partial index) so a retry collides deterministically.
--   * payload_fingerprint   -- sha256 of the import's identity payload (source
--                              metadata + plan/mapping versions + write mode). A
--                              retry with the SAME key but a DIFFERENT fingerprint
--                              is a conflict (app layer raises 409; the DB uniqueness
--                              guarantees a second row cannot masquerade as a retry).
--   * content_hash          -- sha256 of the SOURCE SNAPSHOT content (the file /
--                              sheet bytes-derived digest). Two imports of the same
--                              snapshot share a content_hash -> the unchanged-no-op
--                              path. NULL until the snapshot is read.
--   * execution_id          -- the isolated 042 candidate this import created
--                              (composite-scoped FK). NULL only for a no-op that
--                              created no candidate (unchanged snapshot).
--
-- format is an OPAQUE enum (csv/excel/google_sheets) -- no source branch reads it
-- in core; the WRITER dispatches generically on it (AD-2). write_mode defaults to
-- 'replace' (the only safe mode until 12.9/12.10 add a keyed append contract).
--
-- outcome lifecycle (terminal, closed):
--   'opened'    -> the ledger + candidate exist; rows are being written.
--   'written'   -> rows landed; candidate is 'ready' for the 12.5 DQ/publish gates.
--   'noop'      -> unchanged snapshot: no candidate created, freshness refreshed.
--   'rejected'  -> a blocking rejection threshold breach prevented publication.
--   'published' -> the candidate was published (mirrors the execution reaching
--                  'published'); recorded here for the download/freshness contract.
--   'failed'    -> the import aborted before a candidate could be published.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.managed_feed_import_ledger (
    id                      TEXT        NOT NULL,
    datastream_id           TEXT        NOT NULL,
    project_id              TEXT        NOT NULL,
    -- The isolated 042 candidate this import created. NULL for a no-op (unchanged
    -- snapshot) that created no candidate. Composite-scoped FK below.
    execution_id            TEXT,
    plan_version_id         TEXT        NOT NULL,
    mapping_version_id      TEXT        NOT NULL,
    -- Opaque feed format + write mode. AD-2: no source branch reads these in core.
    feed_format             TEXT        NOT NULL
                            CHECK (feed_format IN ('csv', 'excel', 'google_sheets')),
    write_mode              TEXT        NOT NULL DEFAULT 'replace'
                            CHECK (write_mode IN ('replace', 'append')),
    -- Idempotency + content evidence (all 64-hex sha256, honest-NULL until measured).
    idempotency_key_hash    TEXT        NOT NULL
                            CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    payload_fingerprint     TEXT        NOT NULL
                            CHECK (payload_fingerprint ~ '^[0-9a-f]{64}$'),
    content_hash            TEXT        CHECK (
                                content_hash IS NULL
                                OR content_hash ~ '^[0-9a-f]{64}$'
                            ),
    -- Source snapshot metadata (opaque JSON: filename, sheet, tab, range, revision,
    -- byte size, modified_time, ...). Frozen after insert by the immutability trigger.
    source_metadata         JSONB       NOT NULL DEFAULT '{}'::jsonb
                            CHECK (jsonb_typeof(source_metadata) = 'object'),
    -- The project-scoped RAW landing this import wrote to (schema.table in the
    -- org_<wslug>_raw namespace via open_raw_writer). NULL until rows land / for a
    -- no-op. NEVER a mart (AD-8).
    landing_relation        TEXT,
    -- Honest row-count evidence (AD-9): NULL until measured.
    row_count               BIGINT      CHECK (row_count IS NULL OR row_count >= 0),
    rejected_row_count      BIGINT      NOT NULL DEFAULT 0
                            CHECK (rejected_row_count >= 0),
    -- Terminal lifecycle outcome (see header). 'opened' is the only non-terminal.
    outcome                 TEXT        NOT NULL DEFAULT 'opened'
                            CHECK (outcome IN (
                                'opened', 'written', 'noop',
                                'rejected', 'published', 'failed'
                            )),
    -- When outcome='noop', the prior ledger row whose content this snapshot matched
    -- (freshness lineage: "this run confirmed the snapshot from ledger X is current").
    superseded_ledger_id    TEXT,
    error_code              TEXT,
    error_detail            TEXT,
    -- Honest freshness: the moment the source snapshot was OBSERVED (updated even on
    -- a no-op run so freshness is truthful without duplicating rows).
    snapshot_observed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by              TEXT        NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_managed_feed_import_ledger PRIMARY KEY (id),
    -- House-style ULID id (cf. dse_/dplog_). Crockford base32, 26 chars.
    CONSTRAINT ck_managed_feed_import_ledger_id
        CHECK (id ~ '^mfl_[0-9A-HJKMNP-TV-Z]{26}$'),
    -- Composite uniqueness enables project-scoped FKs FROM the rejected-rows table.
    CONSTRAINT uq_managed_feed_import_ledger_scope
        UNIQUE (id, datastream_id, project_id),
    CONSTRAINT fk_managed_feed_import_ledger_datastream_scope
        FOREIGN KEY (datastream_id, project_id)
        REFERENCES app.datastreams (id, project_id) ON DELETE RESTRICT,
    -- The candidate this import created, project+datastream scoped so a ledger row
    -- cannot bind an execution of another datastream/project (mirrors 042).
    CONSTRAINT fk_managed_feed_import_ledger_execution_scope
        FOREIGN KEY (execution_id, datastream_id, project_id)
        REFERENCES app.datastream_executions (id, datastream_id, project_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_managed_feed_import_ledger_ds
    ON app.managed_feed_import_ledger (datastream_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_managed_feed_import_ledger_project_outcome
    ON app.managed_feed_import_ledger (project_id, outcome);

-- Content-hash lookup for the unchanged-snapshot no-op path: does a PUBLISHED
-- import of this exact snapshot already exist for this datastream?
CREATE INDEX IF NOT EXISTS idx_managed_feed_import_ledger_content
    ON app.managed_feed_import_ledger (datastream_id, content_hash)
    WHERE content_hash IS NOT NULL;

-- Idempotency: at most ONE ledger row per (datastream, idempotency_key_hash). A
-- retry with the same key collides here; the app layer resolves it to the existing
-- row (same payload) or a 409 (different payload). Partial so keyless rows (never
-- expected for managed feeds, but defensive) do not collide.
CREATE UNIQUE INDEX IF NOT EXISTS uq_managed_feed_import_ledger_idempotency
    ON app.managed_feed_import_ledger (datastream_id, idempotency_key_hash)
    WHERE idempotency_key_hash IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Immutability trigger: the ledger is APPEND-ONLY for its identity/evidence, and
-- MONOTONIC for its lifecycle. A row may ONLY be updated to advance the terminal
-- lifecycle (outcome, row_count, rejected_row_count, landing_relation, content_hash
-- once, error_*, execution_id once, superseded_ledger_id, updated_at). Every
-- identity field (datastream/project/plan/mapping/format/write_mode/idempotency/
-- payload_fingerprint/source_metadata/created_by/created_at/snapshot_observed_at)
-- is FROZEN. DELETE is always rejected. Mirrors the frozen-review pattern of
-- 070/071/073.
--
-- Also enforced here (immutability of already-set evidence):
--   * content_hash may go NULL->value ONCE, never value->different value.
--   * execution_id may go NULL->value ONCE, never re-pointed.
--   * a TERMINAL outcome (noop/rejected/published/failed) is frozen: no further
--     lifecycle change. 'written' may still advance to published/failed/rejected.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.protect_managed_feed_import_ledger()
RETURNS TRIGGER AS $$
BEGIN
    IF (TG_OP = 'DELETE') THEN
        RAISE EXCEPTION 'managed_feed_import_ledger is append-only (no DELETE)'
            USING ERRCODE = '23000';
    END IF;

    -- Frozen identity/evidence fields.
    IF NEW.id                   IS DISTINCT FROM OLD.id
       OR NEW.datastream_id        IS DISTINCT FROM OLD.datastream_id
       OR NEW.project_id           IS DISTINCT FROM OLD.project_id
       OR NEW.plan_version_id      IS DISTINCT FROM OLD.plan_version_id
       OR NEW.mapping_version_id   IS DISTINCT FROM OLD.mapping_version_id
       OR NEW.feed_format          IS DISTINCT FROM OLD.feed_format
       OR NEW.write_mode           IS DISTINCT FROM OLD.write_mode
       OR NEW.idempotency_key_hash IS DISTINCT FROM OLD.idempotency_key_hash
       OR NEW.payload_fingerprint  IS DISTINCT FROM OLD.payload_fingerprint
       OR NEW.source_metadata      IS DISTINCT FROM OLD.source_metadata
       OR NEW.created_by           IS DISTINCT FROM OLD.created_by
       OR NEW.created_at           IS DISTINCT FROM OLD.created_at
       OR NEW.snapshot_observed_at IS DISTINCT FROM OLD.snapshot_observed_at
    THEN
        RAISE EXCEPTION 'managed_feed_import_ledger identity/evidence is immutable'
            USING ERRCODE = '23000';
    END IF;

    -- content_hash: NULL->value once; never value->different value.
    IF OLD.content_hash IS NOT NULL
       AND NEW.content_hash IS DISTINCT FROM OLD.content_hash THEN
        RAISE EXCEPTION 'managed_feed_import_ledger content_hash is write-once'
            USING ERRCODE = '23000';
    END IF;

    -- execution_id: NULL->value once; never re-pointed.
    IF OLD.execution_id IS NOT NULL
       AND NEW.execution_id IS DISTINCT FROM OLD.execution_id THEN
        RAISE EXCEPTION 'managed_feed_import_ledger execution_id is write-once'
            USING ERRCODE = '23000';
    END IF;

    -- A terminal outcome is frozen. 'opened' and 'written' may still advance.
    IF OLD.outcome IN ('noop', 'rejected', 'published', 'failed')
       AND NEW.outcome IS DISTINCT FROM OLD.outcome THEN
        RAISE EXCEPTION 'managed_feed_import_ledger outcome % is terminal', OLD.outcome
            USING ERRCODE = '23000';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_managed_feed_import_ledger_protect
    ON app.managed_feed_import_ledger;
CREATE TRIGGER trg_managed_feed_import_ledger_protect
    BEFORE UPDATE OR DELETE ON app.managed_feed_import_ledger
    FOR EACH ROW EXECUTE FUNCTION app.protect_managed_feed_import_ledger();

-- ---------------------------------------------------------------------------
-- app.managed_feed_rejected_rows -- per-row rejection storage (Story 12.8 AC4).
--
-- One row per REJECTED source row, traceable to field + rule + row_number +
-- execution + ledger. Downloadable (the download contract joins this by
-- ledger_id / execution_id). This is a project-scoped toorow-owned table, NOT a
-- mart (AD-8: dbt never writes it). It extends -- does not replace -- migration
-- 026's aggregate rejected_rows COUNT (that count also lives on the ledger row as
-- rejected_row_count, so a blocking threshold decision reads one row).
--
-- Append-only: a rejection record is durable evidence and is never mutated. It is
-- INSERTed inside the same import transaction that writes the ledger row's
-- rejected_row_count so the count and the detail agree.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.managed_feed_rejected_rows (
    id                      BIGSERIAL,
    ledger_id               TEXT        NOT NULL,
    execution_id            TEXT,
    datastream_id           TEXT        NOT NULL,
    project_id              TEXT        NOT NULL,
    -- 0-based source row ordinal (the physical row in the feed). Traceability.
    -- NULL = no row ordinal applies (e.g. a whole-file/shape failure) -- kept
    -- DISTINCT from a real rejection at physical row 0 (M3).
    row_number              BIGINT      CHECK (row_number IS NULL OR row_number >= 0),
    -- The field that failed (NULL when the whole row failed, e.g. bad shape).
    field_name              TEXT,
    -- The rule id that rejected the row (e.g. 'unparseable_date', 'not_null',
    -- 'type_mismatch'). Opaque string; the taxonomy is the WRITER's concern.
    rule                    TEXT        NOT NULL,
    -- Human-readable reason + the offending (already-redacted/truncated) value.
    reason                  TEXT        NOT NULL,
    rejected_value          TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_managed_feed_rejected_rows PRIMARY KEY (id),
    -- Project+datastream-scoped FK to the ledger row (mirrors the 042 scope FKs).
    CONSTRAINT fk_managed_feed_rejected_rows_ledger_scope
        FOREIGN KEY (ledger_id, datastream_id, project_id)
        REFERENCES app.managed_feed_import_ledger (id, datastream_id, project_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_managed_feed_rejected_rows_ledger
    ON app.managed_feed_rejected_rows (ledger_id, row_number);

CREATE INDEX IF NOT EXISTS idx_managed_feed_rejected_rows_execution
    ON app.managed_feed_rejected_rows (execution_id)
    WHERE execution_id IS NOT NULL;

CREATE OR REPLACE FUNCTION app.protect_managed_feed_rejected_rows()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'managed_feed_rejected_rows is append-only'
        USING ERRCODE = '23000';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_managed_feed_rejected_rows_immutable
    ON app.managed_feed_rejected_rows;
CREATE TRIGGER trg_managed_feed_rejected_rows_immutable
    BEFORE UPDATE OR DELETE ON app.managed_feed_rejected_rows
    FOR EACH ROW EXECUTE FUNCTION app.protect_managed_feed_rejected_rows();

-- ---------------------------------------------------------------------------
-- app.project_preferences: the blocking rejection threshold (project-scoped, not a
-- platform-wide hardcode -- "prefer per-project preferences"). Additive, NULL-able,
-- same pattern as 033/042.
--   max_rejected_row_pct : DECIMAL(5,2) NULL -- when the fraction of rejected rows
--       (rejected / (rejected + accepted)) EXCEEDS this percent, publication is
--       BLOCKED (rejection_threshold_exceeded). NULL => documented default in
--       managed_feed_ledger.py (25.00). 0 => any rejection blocks.
-- ---------------------------------------------------------------------------
ALTER TABLE app.project_preferences
    ADD COLUMN IF NOT EXISTS max_rejected_row_pct DECIMAL(5, 2);

ALTER TABLE app.project_preferences
    DROP CONSTRAINT IF EXISTS chk_project_preferences_max_rejected_row_pct;
ALTER TABLE app.project_preferences
    ADD CONSTRAINT chk_project_preferences_max_rejected_row_pct
    CHECK (
        max_rejected_row_pct IS NULL
        OR (max_rejected_row_pct >= 0 AND max_rejected_row_pct <= 100)
    );

COMMIT;
