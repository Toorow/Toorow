-- Story 36.4: one-time authenticated invitation exchange and acceptance.
BEGIN;

ALTER TABLE app.invitations
    ADD COLUMN IF NOT EXISTS bearer_consumed_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS app.invitation_exchange_sessions (
    id                    TEXT        PRIMARY KEY,
    invitation_id         TEXT        NOT NULL
                          REFERENCES app.invitations(id) ON DELETE RESTRICT,
    verified_subject_hash TEXT        NOT NULL
                          CHECK (verified_subject_hash ~ '^[0-9a-f]{64}$'),
    session_hash          TEXT        NOT NULL UNIQUE
                          CHECK (session_hash ~ '^[0-9a-f]{64}$'),
    expires_at            TIMESTAMPTZ NOT NULL,
    consumed_at           TIMESTAMPTZ,
    accepted_operation_id TEXT
                          REFERENCES app.operations(id) ON DELETE RESTRICT,
    resolution            JSONB,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (invitation_id)
);

CREATE INDEX IF NOT EXISTS invitation_exchange_sessions_expiry
    ON app.invitation_exchange_sessions (expires_at)
    WHERE consumed_at IS NULL;

CREATE OR REPLACE FUNCTION app.protect_invitation_exchange_binding()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.invitation_id IS DISTINCT FROM OLD.invitation_id
       OR NEW.verified_subject_hash IS DISTINCT FROM OLD.verified_subject_hash
       OR NEW.session_hash IS DISTINCT FROM OLD.session_hash
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN
        RAISE EXCEPTION 'invitation exchange binding is immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_invitation_exchange_binding_immutable
    ON app.invitation_exchange_sessions;
CREATE TRIGGER trg_invitation_exchange_binding_immutable
    BEFORE UPDATE ON app.invitation_exchange_sessions
    FOR EACH ROW EXECUTE FUNCTION app.protect_invitation_exchange_binding();

COMMIT;
