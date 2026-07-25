-- infra/nango/migrations/085_connector_verification_runs.sql
--
-- Story 38.4: Verify the Domain Continuously and Test Delivery.
--
-- Introduces `app.connector_verification_runs` -- an APPEND-ONLY ledger that
-- records every automated verification and synthetic delivery outcome for a
-- connector installation. Each run is an immutable evidence row; the app layer
-- drives state transitions via the shared operations spine (migration 060).
--
-- INVARIANTS:
--   AD-2  : zero provider/source vocabulary. The connector is referenced only by
--            its `connector_name` string and opaque `evidence_class` tags. No
--            provider brand or DNS-token value enters this table.
--   E38-NFR03: `blocking_reason` is a SANITIZED human-facing string -- no secret,
--              no DNS token, no signing value. `evidence_hash` is a sha256 digest
--              of evidence class + timestamp only; it is safe to store.
--   APPEND-ONLY: the `protect_connector_verification_run` trigger forbids UPDATE
--            and DELETE. Every new outcome is a new immutable row. Mirrors the
--            pattern of `protect_managed_feed_import_ledger` (077) and
--            `protect_connector_installation` (082).
--   OPERATION FK: `operation_id` binds every run to the durable audit+outbox
--            spine (migration 060). NULL until the durable write completes.
--   SYNTHETIC FLAG: `synthetic_delivery = TRUE` marks test-delivery runs that
--            exercised only the auth seam and wrote no durable receipt/import.
--
-- Additive, idempotent, replayable: every statement uses IF NOT EXISTS /
-- OR REPLACE / DROP TRIGGER IF EXISTS so re-application is a no-op. Mirrors
-- 082/084 style.
--
-- Apply order: after 084 (highest on disk). Orchestrator renames if 085 is taken.
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector \
--     -f /migrations/085_connector_verification_runs.sql

BEGIN;

-- ---------------------------------------------------------------------------
-- app.connector_verification_runs -- immutable evidence ledger.
--
-- `id`                : `cvr_<ULID>` TEXT PK (generated at write time).
-- `installation_id`   : FK to app.connector_installations (must exist).
-- `domain_config_id`  : FK to app.connector_domain_configs (the config that
--                       was active at the time of this run). NULL if none set yet.
-- `environment`       : deployment environment name (e.g. 'production').
-- `connector_name`    : stable connector identifier string (AD-2: opaque name).
-- `outcome`           : 'passed' | 'failed' | 'degraded'.
-- `evidence_class`    : opaque evidence tag (e.g. 'routing_check', 'auth_check').
--                       Never a raw secret or DNS token value.
-- `evidence_hash`     : sha256 digest of evidence shape (class + timestamp,
--                       never raw secrets). NULL when not computable.
-- `blocking_reason`   : sanitized human-facing reason on failure. No secret.
--                       NULL on pass.
-- `first_seen_at`     : timestamp when this failure condition was first observed;
--                       carried forward from a prior degraded run. NULL on pass.
-- `ttl_seconds`       : recommended re-check interval in seconds. NULL = default.
-- `synthetic_delivery`: TRUE when this run exercised the auth seam in synthetic
--                       (test) mode only -- no durable receipt or import was written.
-- `operation_id`      : FK to app.operations (the durable write). NULL until set.
-- `created_at`        : immutable insert timestamp.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.connector_verification_runs (
    id                  TEXT        NOT NULL,
    installation_id     TEXT        NOT NULL
                        REFERENCES app.connector_installations(id) ON DELETE RESTRICT,
    domain_config_id    TEXT
                        REFERENCES app.connector_domain_configs(id) ON DELETE RESTRICT,
    environment         TEXT        NOT NULL,
    connector_name      TEXT        NOT NULL,
    outcome             TEXT        NOT NULL
                        CHECK (outcome IN ('passed', 'failed', 'degraded')),
    evidence_class      TEXT        NOT NULL,
    evidence_hash       TEXT,
    blocking_reason     TEXT,
    first_seen_at       TIMESTAMPTZ,
    ttl_seconds         INT,
    synthetic_delivery  BOOLEAN     NOT NULL DEFAULT FALSE,
    operation_id        TEXT
                        REFERENCES app.operations(id) ON DELETE RESTRICT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_connector_verification_runs
        PRIMARY KEY (id),
    CONSTRAINT ck_connector_verification_runs_id
        CHECK (id ~ '^cvr_[0-9A-HJKMNP-TV-Z]{26}$')
);

-- "Latest run per (environment, connector_name)": supports the read-model
-- that returns the most recent verification result for each connector slot.
-- Descending created_at so a LIMIT 1 query on this index is an index scan.
CREATE INDEX IF NOT EXISTS idx_connector_verification_runs_latest
    ON app.connector_verification_runs (environment, connector_name, created_at DESC);

-- Lookup by installation (all runs for one installation).
CREATE INDEX IF NOT EXISTS idx_connector_verification_runs_installation
    ON app.connector_verification_runs (installation_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- Immutability trigger: `protect_connector_verification_run`.
--
-- Every row is an immutable evidence record. Once written it may never be
-- modified or deleted; a new outcome is always a new row (append-only).
-- Mirrors `protect_managed_feed_import_ledger` (077) exactly.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.protect_connector_verification_run()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF (TG_OP = 'DELETE') THEN
        RAISE EXCEPTION 'connector_verification_runs is append-only (no DELETE)'
            USING ERRCODE = '23000';
        RETURN OLD;  -- F9: RETURN OLD (not NEW) on DELETE; NEW is NULL on DELETE.
    END IF;
    IF (TG_OP = 'UPDATE') THEN
        RAISE EXCEPTION 'connector_verification_runs is append-only (no UPDATE)'
            USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_connector_verification_run_protect
    ON app.connector_verification_runs;
CREATE TRIGGER trg_connector_verification_run_protect
    BEFORE UPDATE OR DELETE ON app.connector_verification_runs
    FOR EACH ROW EXECUTE FUNCTION app.protect_connector_verification_run();

COMMIT;
