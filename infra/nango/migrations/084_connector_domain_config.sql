-- infra/nango/migrations/084_connector_domain_config.sql
--
-- Story 38.3: Configure the Inbound Domain and Provider Route.
--
-- Introduces `app.connector_domain_configs` -- a versioned, append-only table
-- that records the receiving domain and delivery adapter id for an installed
-- connector in a given deployment environment.
--
-- INVARIANTS:
--   AD-2  : zero provider/source vocabulary. `provider_adapter` is an opaque id
--            string only. No adapter brand name enters this table, its constraints,
--            or any trigger body.
--   NFR20 : every configure write is durable via the shared `app.operations` /
--            `app.audit_log` / `app.operation_outbox` spine (migration 060). The
--            `operation_id` FK binds each row to that spine.
--   VERSIONED: `config_version` increments monotonically; the prior active row is
--            superseded (superseded_by set) before a new row is inserted. Superseded
--            rows are immutable -- the trigger freezes identity + evidence columns and
--            forbids DELETE (append-only).
--   UNIQUE(domain) on active rows: a domain may bind to exactly one environment per
--            connector. A partial unique index on non-superseded rows enforces this.
--   ONE ACTIVE per (environment, connector_name): a partial unique index enforces
--            that at most one non-superseded config row exists per environment+connector.
--   secret-free: `signing_secret_ref` is a Secret Manager reference id only, NEVER
--            the secret value (E38-NFR03).
--
-- Additive, idempotent, replayable: every statement uses IF NOT EXISTS / OR REPLACE /
-- DROP TRIGGER IF EXISTS so re-application is a no-op. Mirrors 082/077/075 style.
--
-- Apply order: after 083 (highest on disk). Orchestrator renames if 084 is taken.
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/084_connector_domain_config.sql

BEGIN;

-- ---------------------------------------------------------------------------
-- app.connector_domain_configs -- one versioned row per configure call.
--
-- `id`                   : `cdc_<ULID>` TEXT PK (generated at write time, never hashed).
-- `installation_id`      : FK to app.connector_installations (must exist).
-- `environment`          : deployment environment name (e.g. 'production', 'staging').
-- `connector_name`       : stable connector identifier string (AD-2: opaque name).
-- `domain`               : the fully qualified domain being configured.
-- `provider_adapter`     : opaque adapter id string (e.g. 'mailgun_eu' style, but
--                          the CHECK here does NOT assert provider vocabulary --
--                          the app layer holds the allow-list). AD-2.
-- `webhook_endpoint_version`: version tag for the webhook endpoint path.
-- `signing_secret_ref`   : Secret Manager reference id (AD-2 / E38-NFR03).
--                          NEVER the secret value. NULL until set.
-- `dns_evidence_class`   : evidence class tag (e.g. 'spf_dkim_dmarc'). NULL = unknown.
-- `dns_evidence_hash`    : deterministic hash/fingerprint of DNS evidence state.
--                          NULL until a verification cycle provides it (38.4).
-- `config_version`       : monotonically increasing integer per installation.
-- `superseded_by`        : id of the row that replaced this one, or NULL if active.
-- `operation_id`         : FK to app.operations (the durable write). NULL until wired.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.connector_domain_configs (
    id                       TEXT        NOT NULL,
    installation_id          TEXT        NOT NULL
                             REFERENCES app.connector_installations(id) ON DELETE RESTRICT,
    environment              TEXT        NOT NULL,
    connector_name           TEXT        NOT NULL,
    domain                   TEXT        NOT NULL,
    provider_adapter         TEXT        NOT NULL,
    webhook_endpoint_version TEXT        NOT NULL DEFAULT 'v1',
    signing_secret_ref       TEXT,
    dns_evidence_class       TEXT,
    dns_evidence_hash        TEXT,
    config_version           INT         NOT NULL,
    superseded_by            TEXT,
    operation_id             TEXT        REFERENCES app.operations(id) ON DELETE RESTRICT,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_connector_domain_configs
        PRIMARY KEY (id),
    CONSTRAINT ck_connector_domain_configs_id
        CHECK (id ~ '^cdc_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT ck_connector_domain_configs_version
        CHECK (config_version >= 1)
);

-- One ACTIVE (non-superseded) domain per (environment, connector_name).
-- Partial index: active rows only (superseded_by IS NULL).
CREATE UNIQUE INDEX IF NOT EXISTS uq_connector_domain_configs_active_env_name
    ON app.connector_domain_configs (environment, connector_name)
    WHERE superseded_by IS NULL;

-- One environment per domain on active rows: a domain may not bind to two
-- environments simultaneously (cross-env conflict).
CREATE UNIQUE INDEX IF NOT EXISTS uq_connector_domain_configs_active_domain
    ON app.connector_domain_configs (domain)
    WHERE superseded_by IS NULL;

-- Lookup index: find all config rows for one installation.
CREATE INDEX IF NOT EXISTS idx_connector_domain_configs_installation
    ON app.connector_domain_configs (installation_id, config_version DESC);

-- Lookup index: find the active config for an environment+connector pair.
CREATE INDEX IF NOT EXISTS idx_connector_domain_configs_env_name
    ON app.connector_domain_configs (environment, connector_name, config_version DESC);

-- ---------------------------------------------------------------------------
-- Immutability trigger: `protect_connector_domain_config`.
--
-- Mirrors `protect_connector_installation` (082) and
-- `protect_managed_feed_import_ledger` (077).
--
-- FROZEN identity + evidence columns (may NEVER change after insert):
--   id, installation_id, environment, connector_name, domain,
--   provider_adapter, webhook_endpoint_version, signing_secret_ref,
--   dns_evidence_class, dns_evidence_hash, config_version, created_at
--
-- MUTABLE lifecycle columns (the ONLY columns the app layer may UPDATE):
--   superseded_by, operation_id, updated_at
--
-- DELETE is always rejected (append-only).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.protect_connector_domain_config()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF (TG_OP = 'DELETE') THEN
        RAISE EXCEPTION 'connector_domain_configs is append-only (no DELETE)'
            USING ERRCODE = '23000';
    END IF;

    -- Frozen identity + evidence columns: reject any mutation.
    IF NEW.id                       IS DISTINCT FROM OLD.id
       OR NEW.installation_id          IS DISTINCT FROM OLD.installation_id
       OR NEW.environment              IS DISTINCT FROM OLD.environment
       OR NEW.connector_name           IS DISTINCT FROM OLD.connector_name
       OR NEW.domain                   IS DISTINCT FROM OLD.domain
       OR NEW.provider_adapter         IS DISTINCT FROM OLD.provider_adapter
       OR NEW.webhook_endpoint_version IS DISTINCT FROM OLD.webhook_endpoint_version
       OR NEW.signing_secret_ref       IS DISTINCT FROM OLD.signing_secret_ref
       OR NEW.dns_evidence_class       IS DISTINCT FROM OLD.dns_evidence_class
       OR NEW.dns_evidence_hash        IS DISTINCT FROM OLD.dns_evidence_hash
       OR NEW.config_version           IS DISTINCT FROM OLD.config_version
       OR NEW.created_at               IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'connector_domain_configs identity/evidence columns are immutable'
            USING ERRCODE = '23000';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_connector_domain_config_protect
    ON app.connector_domain_configs;
CREATE TRIGGER trg_connector_domain_config_protect
    BEFORE UPDATE OR DELETE ON app.connector_domain_configs
    FOR EACH ROW EXECUTE FUNCTION app.protect_connector_domain_config();

COMMIT;
