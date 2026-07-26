-- Story 43.10: one-time self-hosted instance claim foundation.
--
-- The installer bearer is exchanged before authentication. Only SHA-256
-- digests of the installer bearer and short-lived tokenless exchange session are
-- persisted. Claim state, first owners and first tenant scope use the existing
-- app.operations/audit_log/operation_outbox transaction spine.

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS app.instance_bootstrap_capabilities (
    id                    TEXT        PRIMARY KEY,
    bearer_hash           TEXT        NOT NULL UNIQUE CHECK (bearer_hash ~ '^[0-9a-f]{64}$'),
    state                 TEXT        NOT NULL DEFAULT 'active'
                                  CHECK (state IN ('active', 'exchanged', 'consumed', 'revoked')),
    expires_at            TIMESTAMPTZ NOT NULL,
    exchanged_at          TIMESTAMPTZ,
    consumed_at           TIMESTAMPTZ,
    consumed_by_person_id TEXT REFERENCES app.persons(id) ON DELETE RESTRICT,
    revoked_at            TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (expires_at > created_at),
    CHECK (
        (state = 'active' AND exchanged_at IS NULL AND consumed_at IS NULL
                          AND consumed_by_person_id IS NULL AND revoked_at IS NULL)
        OR (state = 'exchanged' AND exchanged_at IS NOT NULL AND consumed_at IS NULL
                                AND consumed_by_person_id IS NULL AND revoked_at IS NULL)
        OR (state = 'consumed' AND exchanged_at IS NOT NULL AND consumed_at IS NOT NULL
                               AND consumed_by_person_id IS NOT NULL AND revoked_at IS NULL)
        OR (state = 'revoked' AND consumed_at IS NULL
                              AND consumed_by_person_id IS NULL AND revoked_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS instance_bootstrap_one_pending
    ON app.instance_bootstrap_capabilities ((1))
    WHERE state IN ('active', 'exchanged');
CREATE INDEX IF NOT EXISTS instance_bootstrap_expiry
    ON app.instance_bootstrap_capabilities (expires_at)
    WHERE state IN ('active', 'exchanged');

CREATE TABLE IF NOT EXISTS app.instance_bootstrap_exchange_sessions (
    id                      TEXT        PRIMARY KEY,
    bootstrap_capability_id TEXT        NOT NULL UNIQUE
                                     REFERENCES app.instance_bootstrap_capabilities(id)
                                     ON DELETE RESTRICT,
    session_hash            TEXT        NOT NULL UNIQUE
                                     CHECK (session_hash ~ '^[0-9a-f]{64}$'),
    state                   TEXT        NOT NULL DEFAULT 'active'
                                     CHECK (state IN ('active', 'consumed', 'revoked')),
    expires_at              TIMESTAMPTZ NOT NULL,
    consumed_at             TIMESTAMPTZ,
    consumed_by_person_id   TEXT REFERENCES app.persons(id) ON DELETE RESTRICT,
    revoked_at              TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (expires_at > created_at),
    CHECK (
        (state = 'active' AND consumed_at IS NULL
                          AND consumed_by_person_id IS NULL AND revoked_at IS NULL)
        OR (state = 'consumed' AND consumed_at IS NOT NULL
                               AND consumed_by_person_id IS NOT NULL AND revoked_at IS NULL)
        OR (state = 'revoked' AND consumed_at IS NULL
                              AND consumed_by_person_id IS NULL AND revoked_at IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS instance_bootstrap_exchange_expiry
    ON app.instance_bootstrap_exchange_sessions (expires_at) WHERE state = 'active';

CREATE TABLE IF NOT EXISTS app.instance_members (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES app.persons(id) ON DELETE RESTRICT,
    role TEXT NOT NULL CHECK (role IN ('owner', 'admin')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (person_id)
);

CREATE TABLE IF NOT EXISTS app.instance_claims (
    singleton_key SMALLINT PRIMARY KEY DEFAULT 1 CHECK (singleton_key = 1),
    id TEXT NOT NULL UNIQUE,
    bootstrap_capability_id TEXT NOT NULL UNIQUE
        REFERENCES app.instance_bootstrap_capabilities(id) ON DELETE RESTRICT,
    owner_person_id TEXT NOT NULL REFERENCES app.persons(id) ON DELETE RESTRICT,
    org_id TEXT NOT NULL UNIQUE REFERENCES app.organizations(id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    project_id TEXT NOT NULL UNIQUE REFERENCES app.projects(id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    operation_id TEXT NOT NULL UNIQUE REFERENCES app.operations(id) ON DELETE RESTRICT,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE OR REPLACE FUNCTION app.protect_instance_bootstrap_capability()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.bearer_hash IS DISTINCT FROM OLD.bearer_hash
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'bootstrap capability binding is immutable';
    END IF;
    IF OLD.state IN ('consumed', 'revoked') THEN
        RAISE EXCEPTION 'terminal bootstrap capability is immutable';
    END IF;
    IF (OLD.state = 'active' AND NEW.state NOT IN ('exchanged', 'revoked'))
       OR (OLD.state = 'exchanged' AND NEW.state NOT IN ('consumed', 'revoked')) THEN
        RAISE EXCEPTION 'invalid bootstrap capability transition';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_instance_bootstrap_capability ON app.instance_bootstrap_capabilities;
CREATE TRIGGER trg_instance_bootstrap_capability BEFORE UPDATE
    ON app.instance_bootstrap_capabilities
    FOR EACH ROW EXECUTE FUNCTION app.protect_instance_bootstrap_capability();

CREATE OR REPLACE FUNCTION app.protect_instance_bootstrap_exchange()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.bootstrap_capability_id IS DISTINCT FROM OLD.bootstrap_capability_id
       OR NEW.session_hash IS DISTINCT FROM OLD.session_hash
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'bootstrap exchange binding is immutable';
    END IF;
    IF OLD.state <> 'active' THEN
        RAISE EXCEPTION 'terminal bootstrap exchange is immutable';
    END IF;
    IF NEW.state NOT IN ('consumed', 'revoked') THEN
        RAISE EXCEPTION 'invalid bootstrap exchange transition';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_instance_bootstrap_exchange
    ON app.instance_bootstrap_exchange_sessions;
CREATE TRIGGER trg_instance_bootstrap_exchange BEFORE UPDATE
    ON app.instance_bootstrap_exchange_sessions
    FOR EACH ROW EXECUTE FUNCTION app.protect_instance_bootstrap_exchange();

CREATE OR REPLACE FUNCTION app.protect_instance_claim()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'instance claim is immutable';
END;
$$;
DROP TRIGGER IF EXISTS trg_instance_claim_immutable ON app.instance_claims;
CREATE TRIGGER trg_instance_claim_immutable BEFORE UPDATE OR DELETE ON app.instance_claims
    FOR EACH ROW EXECUTE FUNCTION app.protect_instance_claim();

COMMIT;