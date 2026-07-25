-- Story 36.7: delegated source authorization and exact account exposure.
-- Additive and replayable. Provider/account identifiers remain opaque; NO token
-- or secret is ever stored here -- only identifiers, one-way keyed hashes and the
-- public PKCE challenge.

BEGIN;

CREATE TABLE IF NOT EXISTS app.source_delegations (
    delegation_id           TEXT PRIMARY KEY,
    task_id                 TEXT NOT NULL REFERENCES app.setup_tasks(id) ON DELETE RESTRICT,
    source                  TEXT NOT NULL,
    provider                TEXT NOT NULL,
    owner_org_id            TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    beneficiary_org_id      TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    requested_scopes        JSONB NOT NULL CHECK (jsonb_typeof(requested_scopes) = 'array'),
    state                   TEXT NOT NULL DEFAULT 'prepared' CHECK (state IN (
                                'prepared', 'authorized', 'exposed',
                                'failed', 'expired', 'revoked'
                            )),
    -- One-way keyed hash of the anti-CSRF nonce carried by the HMAC state.
    nonce_hash              TEXT CHECK (nonce_hash IS NULL OR nonce_hash ~ '^[0-9a-f]{64}$'),
    -- PKCE (RFC 7636). The challenge (public) is stored for non-Google-direct
    -- providers; the verifier is server-only and persisted ONLY as a keyed hash.
    pkce_challenge          TEXT,
    pkce_method             TEXT CHECK (pkce_method IS NULL OR pkce_method = 'S256'),
    pkce_verifier_hash      TEXT CHECK (
                                pkce_verifier_hash IS NULL
                                OR pkce_verifier_hash ~ '^[0-9a-f]{64}$'
                            ),
    -- Reference to the server-owned exact redirect allow-list (never a client value).
    redirect_allowlist_ref  TEXT,
    expires_at              TIMESTAMPTZ NOT NULL,
    operation_id            TEXT REFERENCES app.operations(id) ON DELETE RESTRICT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Non-Google-direct providers MUST carry a PKCE challenge; Google-direct
    -- reuse google_oauth state+nonce and carry none (contract check).
    CONSTRAINT source_delegations_pkce_present CHECK (
        pkce_challenge IS NULL OR pkce_method = 'S256'
    ),
    CONSTRAINT uq_source_delegation_operation UNIQUE (operation_id)
);

CREATE INDEX IF NOT EXISTS source_delegations_task_living
    ON app.source_delegations (task_id, expires_at)
    WHERE state = 'prepared';

CREATE INDEX IF NOT EXISTS source_delegations_beneficiary
    ON app.source_delegations (beneficiary_org_id, updated_at DESC);

-- Server-owned exact redirect allow-list. Callback verification matches the
-- presented redirect_uri EXACTLY against active rows for the delegation's
-- allowlist_ref -- no prefix/substring match, never a client-supplied value.
CREATE TABLE IF NOT EXISTS app.source_delegation_redirects (
    id                  TEXT PRIMARY KEY,
    allowlist_ref       TEXT NOT NULL,
    redirect_uri        TEXT NOT NULL,
    active              BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_source_delegation_redirect UNIQUE (allowlist_ref, redirect_uri)
);

CREATE INDEX IF NOT EXISTS source_delegation_redirects_active
    ON app.source_delegation_redirects (allowlist_ref)
    WHERE active;

-- The security-critical binding columns are immutable after preparation: only
-- `state` and `updated_at` may change (transition machine). This mirrors 065's
-- protect_setup_handoff_binding.
CREATE OR REPLACE FUNCTION app.protect_source_delegation_binding()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.task_id IS DISTINCT FROM OLD.task_id
       OR NEW.source IS DISTINCT FROM OLD.source
       OR NEW.provider IS DISTINCT FROM OLD.provider
       OR NEW.owner_org_id IS DISTINCT FROM OLD.owner_org_id
       OR NEW.beneficiary_org_id IS DISTINCT FROM OLD.beneficiary_org_id
       OR NEW.requested_scopes IS DISTINCT FROM OLD.requested_scopes
       OR NEW.nonce_hash IS DISTINCT FROM OLD.nonce_hash
       OR NEW.pkce_challenge IS DISTINCT FROM OLD.pkce_challenge
       OR NEW.pkce_method IS DISTINCT FROM OLD.pkce_method
       OR NEW.pkce_verifier_hash IS DISTINCT FROM OLD.pkce_verifier_hash
       OR NEW.redirect_allowlist_ref IS DISTINCT FROM OLD.redirect_allowlist_ref
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
       OR NEW.operation_id IS DISTINCT FROM OLD.operation_id THEN
        RAISE EXCEPTION 'source delegation binding is immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_source_delegation_binding_immutable ON app.source_delegations;
CREATE TRIGGER trg_source_delegation_binding_immutable
    BEFORE UPDATE ON app.source_delegations
    FOR EACH ROW EXECUTE FUNCTION app.protect_source_delegation_binding();

COMMIT;
