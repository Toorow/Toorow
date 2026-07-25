-- infra/nango/migrations/087_connector_activations.sql
--
-- Story 38.5: Activate the Connector for a Tenant.
--
-- Introduces `app.connector_activations` -- the per-org, per-connector,
-- per-environment activation record that tracks whether a tenant organisation
-- has activated a READY connector for use in inbound Datastreams.
--
-- INVARIANTS:
--   AD-2  : zero provider/source vocabulary. The connector is referenced only
--            by its `connector_name` string. No provider brand, adapter name,
--            or inbound-delivery vocabulary enters this table.
--   AD-5  : tenant isolation -- `org_id` is the authoritative scope. One row
--            per (org_id, connector_name, environment). Cross-org reads are
--            forbidden at the app layer (nondisclosing 404).
--   DATA-PRESERVATION (AC3): deactivation is a state change (UPDATE) never a
--            DELETE. Retained raw evidence, audit history and last-known-good
--            publications are untouched by deactivation.
--   IMMUTABILITY TRIGGER: `protect_connector_activation` freezes identity
--            columns (`org_id`, `connector_name`, `environment`, `id`,
--            `created_at`, `activated_by`) and permits only `state`,
--            `deactivated_at`, `operation_id`, `updated_at` to change. DELETE
--            is forbidden (returns OLD, logged).
--   OPERATION FK: `operation_id` binds each write to the shared durable
--            audit + outbox spine (migration 060). NULL until the durable
--            write completes.
--   UNIQUE(org_id, connector_name, environment): one activation record per
--            org+connector+environment. The app layer uses INSERT ... ON
--            CONFLICT + rowcount reconciliation for deterministic idempotency.
--
-- Additive, idempotent, replayable: every statement uses IF NOT EXISTS /
-- OR REPLACE / DROP TRIGGER IF EXISTS so re-application is a no-op.
-- Mirrors 082/084/085 style.
--
-- Apply order: after 086 (highest on disk). Orchestrator renames if 087 is taken.
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector \
--     -f /migrations/087_connector_activations.sql

BEGIN;

-- ---------------------------------------------------------------------------
-- app.connector_activations -- org-scoped activation record.
--
-- `id`              : `cac_<ULID>` TEXT PK (generated at write time).
-- `org_id`          : organisation identifier (tenant scope; AD-5).
-- `connector_name`  : stable connector identifier string (AD-2: opaque name).
-- `environment`     : deployment environment name (e.g. 'production').
-- `state`           : 'ACTIVE' | 'DEACTIVATED'.
-- `activated_by`    : identity of the org-owner who performed activation.
-- `activated_at`    : timestamp of initial activation. Immutable after insert.
-- `deactivated_at`  : timestamp of most recent deactivation. NULL when ACTIVE.
-- `operation_id`    : FK to app.operations (the durable write). NULL until set.
-- `created_at`      : immutable row-creation timestamp.
-- `updated_at`      : mutable: set on every allowed state update.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.connector_activations (
    id                TEXT        NOT NULL,
    org_id            TEXT        NOT NULL,
    connector_name    TEXT        NOT NULL,
    environment       TEXT        NOT NULL,
    state             TEXT        NOT NULL
                      CHECK (state IN ('ACTIVE', 'DEACTIVATED')),
    activated_by      TEXT        NOT NULL,
    activated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deactivated_at    TIMESTAMPTZ,
    operation_id      TEXT
                      REFERENCES app.operations(id) ON DELETE RESTRICT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_connector_activations
        PRIMARY KEY (id),
    CONSTRAINT ck_connector_activations_id
        CHECK (id ~ '^cac_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_connector_activations_org_connector_env
        UNIQUE (org_id, connector_name, environment)
);

-- Index: lookup by org (common read path for activation status per org).
CREATE INDEX IF NOT EXISTS idx_connector_activations_org
    ON app.connector_activations (org_id, connector_name, environment);

-- Index: lookup by connector_name + environment (health-surface aggregation).
CREATE INDEX IF NOT EXISTS idx_connector_activations_connector_env
    ON app.connector_activations (connector_name, environment);

-- ---------------------------------------------------------------------------
-- Immutability trigger: `protect_connector_activation`.
--
-- Identity columns (org_id, connector_name, environment, id, created_at,
-- activated_by) are FROZEN after insert. Only the mutable columns
-- (state, deactivated_at, operation_id, updated_at) may change.
-- DELETE is forbidden unconditionally -- data-preservation invariant (AC3).
-- Mirrors the protect_connector_installation trigger style (migration 082).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.protect_connector_activation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF (TG_OP = 'DELETE') THEN
        RAISE EXCEPTION
            'connector_activations rows may not be deleted -- deactivate instead'
            USING ERRCODE = '23000';
        RETURN OLD;
    END IF;
    IF (TG_OP = 'UPDATE') THEN
        -- Freeze identity columns.
        IF NEW.id             IS DISTINCT FROM OLD.id             THEN
            RAISE EXCEPTION 'connector_activations.id is immutable'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.org_id         IS DISTINCT FROM OLD.org_id         THEN
            RAISE EXCEPTION 'connector_activations.org_id is immutable'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.connector_name IS DISTINCT FROM OLD.connector_name THEN
            RAISE EXCEPTION 'connector_activations.connector_name is immutable'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.environment    IS DISTINCT FROM OLD.environment    THEN
            RAISE EXCEPTION 'connector_activations.environment is immutable'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.created_at     IS DISTINCT FROM OLD.created_at     THEN
            RAISE EXCEPTION 'connector_activations.created_at is immutable'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.activated_by   IS DISTINCT FROM OLD.activated_by   THEN
            RAISE EXCEPTION 'connector_activations.activated_by is immutable'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.activated_at   IS DISTINCT FROM OLD.activated_at   THEN
            RAISE EXCEPTION 'connector_activations.activated_at is immutable'
                USING ERRCODE = '23000';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_connector_activation_protect
    ON app.connector_activations;
CREATE TRIGGER trg_connector_activation_protect
    BEFORE UPDATE OR DELETE ON app.connector_activations
    FOR EACH ROW EXECUTE FUNCTION app.protect_connector_activation();

COMMIT;
