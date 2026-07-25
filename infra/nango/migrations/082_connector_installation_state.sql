-- infra/nango/migrations/082_connector_installation_state.sql
--
-- Story 38.2: Installation readiness as independent, audited connector state.
--
-- Introduces `app.connector_installations` -- a per-environment/per-connector
-- state row that tracks whether a toorow platform connector (identified only by
-- its `connector_name` string -- AD-2: zero provider vocabulary here) is
-- operationally ready for tenants to activate.
--
-- States (transition machine, enforced by both the app layer and the CHECK):
--   NOT_INSTALLED -> DOMAIN_PENDING -> VERIFYING -> READY
--                                                 -> DEGRADED <-> READY
--   any state    -> DISABLED
--
-- INVARIANTS:
--   AD-2  : zero provider/source vocabulary. The connector is referenced only by
--            its `connector_name` string; no Mailgun/Cloudflare/SMTP vocabulary
--            enters this table or any of its constraints.
--   AD-9  : `blocking_cause` is a human-facing SANITIZED string (no secret, no
--            credential, no cross-tenant infrastructure detail).
--   NFR20 : every transition is a durable, queryable, append-only audit record via
--            the shared `app.operations` / `app.audit_log` / `app.operation_outbox`
--            spine (migration 060). The `operation_id` FK binds each row to that
--            durable spine.
--   UNIQUE(environment, connector_name): at most ONE installation row per
--            environment/connector pair; a retry with the same Idempotency-Key
--            returns the existing row (app layer idempotency, seam: operations.py).
--
-- Additive, idempotent, replayable: every statement is IF NOT EXISTS / OR REPLACE /
-- constraint-guarded so re-application is a no-op. Mirrors 075/077 style.
--
-- Apply order: after 081 (highest applied). Coordinate with the parallel Epic 12
-- agent: the orchestrator owns the final number and renames if 082 is taken.
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/082_connector_installation_state.sql

BEGIN;

-- ---------------------------------------------------------------------------
-- app.connector_installations -- one row per (environment, connector_name).
--
-- `id`               : `cin_<ULID>` TEXT PK (crockford base32, 26 chars).
-- `environment`      : deployment environment name (e.g. 'production', 'staging').
-- `connector_name`   : stable connector identifier string (e.g. 'inbound-managed-file').
--                      AD-2: opaque name, never a provider/brand identifier.
-- `state`            : the 6-state installation lifecycle (inline CHECK, not a
--                      pg TYPE -- mirrors 075/077/068 convention).
-- `blocking_cause`   : sanitized human-facing description (no secret, no infra
--                      detail, no cross-tenant data). NULL when not blocked.
-- `responsible_actor`: opaque class of the actor responsible for next action
--                      (e.g. 'platform_admin', 'domain_config', 'automated').
--                      NULL until set.
-- `last_verified_at` : timestamp of the last successful automated verification.
--                      NULL until a VERIFYING -> READY transition occurs.
-- `operation_id`     : FK to app.operations (the durable write that created or
--                      last transitioned this row). NULL until first write via
--                      the operations spine.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.connector_installations (
    id                  TEXT        NOT NULL,
    environment         TEXT        NOT NULL,
    connector_name      TEXT        NOT NULL,
    state               TEXT        NOT NULL DEFAULT 'NOT_INSTALLED'
                        CHECK (state IN (
                            'NOT_INSTALLED',
                            'DOMAIN_PENDING',
                            'VERIFYING',
                            'READY',
                            'DEGRADED',
                            'DISABLED'
                        )),
    -- Sanitized human-facing cause for non-READY states. No secret or infra detail.
    blocking_cause      TEXT,
    -- Actor class responsible for advancing past the current state.
    responsible_actor   TEXT,
    last_verified_at    TIMESTAMPTZ,
    -- FK to the durable operations spine (migration 060). NULL before first write.
    operation_id        TEXT        REFERENCES app.operations(id) ON DELETE RESTRICT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_connector_installations
        PRIMARY KEY (id),
    CONSTRAINT ck_connector_installations_id
        CHECK (id ~ '^cin_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_connector_installations_env_name
        UNIQUE (environment, connector_name)
);

CREATE INDEX IF NOT EXISTS idx_connector_installations_env
    ON app.connector_installations (environment, state, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_connector_installations_connector_name
    ON app.connector_installations (connector_name, state);

-- ---------------------------------------------------------------------------
-- Immutability / transition trigger: `protect_connector_installation`.
--
-- Mirrors `protect_host_preflight_binding` (075) and
-- `protect_managed_feed_import_ledger` (077).
--
-- FROZEN identity columns (may NEVER change after insert):
--   id, environment, connector_name, created_at
--
-- MUTABLE lifecycle columns (the ONLY columns the app layer may UPDATE):
--   state, blocking_cause, responsible_actor, last_verified_at,
--   operation_id, updated_at
--
-- DELETE is always rejected (append-only).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.protect_connector_installation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF (TG_OP = 'DELETE') THEN
        RAISE EXCEPTION 'connector_installations is append-only (no DELETE)'
            USING ERRCODE = '23000';
    END IF;

    -- Frozen identity columns: reject any mutation.
    IF NEW.id              IS DISTINCT FROM OLD.id
       OR NEW.environment     IS DISTINCT FROM OLD.environment
       OR NEW.connector_name  IS DISTINCT FROM OLD.connector_name
       OR NEW.created_at      IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'connector_installations identity columns are immutable'
            USING ERRCODE = '23000';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_connector_installation_protect
    ON app.connector_installations;
CREATE TRIGGER trg_connector_installation_protect
    BEFORE UPDATE OR DELETE ON app.connector_installations
    FOR EACH ROW EXECUTE FUNCTION app.protect_connector_installation();

COMMIT;
