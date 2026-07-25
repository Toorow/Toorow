-- infra/nango/migrations/078_csv_excel_import_contract.sql
--
-- Story 12.9: Import CSV and Excel as governed dataset versions.
--
-- Introduces the VERSIONED REUSABLE parsing/import contract table
-- (``app.csv_excel_import_contracts``) so that the same parsing configuration
-- (format, delimiter, encoding, sheet, header row, date format, locale, …) can
-- be stored, reviewed, and reused across multiple imports of the same datastream
-- without re-supplying it each time (AC2).
--
-- Design decisions:
--   * One row per contract VERSION (append-only; never UPDATE the content once a
--     row is minted -- mirrors the immutability pattern of 030/042/071/073/077).
--     A new file + same config = same contract version used again.
--     A config change = a new version row with a new ``cic_<ULID>`` id.
--   * Scoped to (datastream_id, project_id) so cross-project access is blocked
--     at the DB constraint level.
--   * ``contract`` is a JSONB column carrying the full normalised contract dict
--     produced by ``csv_excel_import.validate_import_contract``.  The application
--     layer owns schema validation; the DB stores the resolved document.
--   * An append-only trigger freezes the immutable identity fields
--     (datastream_id, project_id, format, write_mode, contract, created_by,
--     created_at, fingerprint) on BEFORE UPDATE OR DELETE -- mirrors the 077/073
--     immutability pattern.  ``label`` and ``is_active`` are intentionally
--     mutable (the label is a human annotation; is_active allows deactivation
--     without deletion).
--   * ``fingerprint`` is the SHA-256 of the normalised contract JSON (sorted keys)
--     so the application can detect when a re-submitted contract is identical to an
--     existing version without re-versioning unnecessarily.
--   * ``format`` mirrors the ``managed_feed_import_ledger.feed_format`` enum
--     (csv / excel) -- no extra enum type needed; the CHECK constraint is the guard.
--
-- Additive and idempotent (IF NOT EXISTS / OR REPLACE throughout) -- safe to
-- re-apply after any preceding migration.
--
-- Apply with (human-gated on Jean's authorization):
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector \
--     -f /migrations/078_csv_excel_import_contract.sql

BEGIN;

-- ---------------------------------------------------------------------------
-- app.csv_excel_import_contracts
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.csv_excel_import_contracts (
    -- Identity (immutable after insert).
    id                  TEXT        NOT NULL PRIMARY KEY,  -- cic_<ULID>
    datastream_id       TEXT        NOT NULL
                            REFERENCES app.datastreams (id) ON DELETE RESTRICT,
    project_id          TEXT        NOT NULL,
    fingerprint         TEXT        NOT NULL
                            CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    format              TEXT        NOT NULL
                            CHECK (format IN ('csv', 'excel')),
    write_mode          TEXT        NOT NULL DEFAULT 'replace'
                            CHECK (write_mode = 'replace'),   -- Append blocked in 12.9.
    contract            JSONB       NOT NULL,                  -- normalised contract doc
    created_by          TEXT        NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Mutable (human annotations / lifecycle).
    label               TEXT        NULL,         -- optional operator label
    is_active           BOOLEAN     NOT NULL DEFAULT TRUE,

    -- Scope constraint: only rows for this project are visible to project members.
    CONSTRAINT fk_cic_datastream_scope
        FOREIGN KEY (datastream_id, project_id)
        REFERENCES app.datastreams (id, project_id)
        ON DELETE RESTRICT
);

-- Unique fingerprint per (datastream, project): same config => same version,
-- no duplicate rows for unchanged contracts.
CREATE UNIQUE INDEX IF NOT EXISTS uq_csv_excel_contract_fingerprint
    ON app.csv_excel_import_contracts (datastream_id, project_id, fingerprint);

-- Fast lookup: all versions for a datastream, newest first.
CREATE INDEX IF NOT EXISTS ix_csv_excel_contract_datastream
    ON app.csv_excel_import_contracts (datastream_id, project_id, created_at DESC);

-- Fast lookup: only active contracts.
CREATE INDEX IF NOT EXISTS ix_csv_excel_contract_active
    ON app.csv_excel_import_contracts (datastream_id, project_id)
    WHERE is_active = TRUE;

-- ---------------------------------------------------------------------------
-- Immutability trigger: freeze the identity fields on UPDATE / DELETE.
-- Mirrors the pattern of migrations 030 / 073 / 077 exactly.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION app.reject_csv_excel_contract_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql AS
$$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'csv_excel_import_contracts is append-only: '
            'DELETE of id=% is forbidden (immutable import contract record).',
            OLD.id;
    END IF;
    -- UPDATE: freeze the identity fields; allow only label + is_active changes.
    IF NEW.id              IS DISTINCT FROM OLD.id              OR
       NEW.datastream_id   IS DISTINCT FROM OLD.datastream_id   OR
       NEW.project_id      IS DISTINCT FROM OLD.project_id      OR
       NEW.fingerprint      IS DISTINCT FROM OLD.fingerprint      OR
       NEW.format           IS DISTINCT FROM OLD.format           OR
       NEW.write_mode       IS DISTINCT FROM OLD.write_mode       OR
       NEW.contract         IS DISTINCT FROM OLD.contract         OR
       NEW.created_by       IS DISTINCT FROM OLD.created_by       OR
       NEW.created_at       IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION
            'csv_excel_import_contracts: identity fields are immutable after insert '
            '(id=%). Only label and is_active may be updated.',
            OLD.id;
    END IF;
    RETURN NEW;
END;
$$;

-- Drop and re-create so the trigger body tracks the function body (idempotent).
DROP TRIGGER IF EXISTS trg_csv_excel_contract_immutable
    ON app.csv_excel_import_contracts;

CREATE TRIGGER trg_csv_excel_contract_immutable
    BEFORE UPDATE OR DELETE ON app.csv_excel_import_contracts
    FOR EACH ROW
    EXECUTE FUNCTION app.reject_csv_excel_contract_mutation();

-- ---------------------------------------------------------------------------
-- Ledger FK: add a nullable column to app.managed_feed_import_ledger that
-- references the contract version used for this import.  Additive / idempotent.
-- ---------------------------------------------------------------------------

ALTER TABLE app.managed_feed_import_ledger
    ADD COLUMN IF NOT EXISTS import_contract_id TEXT NULL
        REFERENCES app.csv_excel_import_contracts (id) ON DELETE RESTRICT;

COMMENT ON COLUMN app.managed_feed_import_ledger.import_contract_id IS
    'FK to the csv_excel_import_contracts version used for this import (NULL for '
    'non-CSV/Excel feeds and for 12.8 direct imports that predate 12.9).';

-- ---------------------------------------------------------------------------
-- NOTE (M1/M2): the project_preferences columns this story reads --
--   * allow_empty_publication  -> already added by migration 042, and
--   * max_rejected_row_pct      -> already added by migration 077 (with its named
--                                  CHECK constraint chk_project_preferences_max_rejected_row_pct)
-- are DELIBERATELY NOT re-added here. Re-adding max_rejected_row_pct with a second,
-- ANONYMOUS CHECK constraint would duplicate the guard and drift from 077's named
-- constraint. Migration 078 owns ONLY the csv_excel_import_contracts table and the
-- ledger's import_contract_id FK column. This keeps 078 strictly additive and
-- non-overlapping with 042/077.
-- ---------------------------------------------------------------------------

COMMIT;
