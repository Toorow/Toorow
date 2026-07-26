-- Story 43.2: bind invitation exchange sessions to the canonical person.
-- Existing short-lived sessions remain nullable and cannot gain authority; a
-- canonical acceptance requires the exact person bound during a new exchange.

BEGIN;

ALTER TABLE app.invitation_exchange_sessions
    ADD COLUMN IF NOT EXISTS person_id TEXT
    REFERENCES app.persons(id) ON DELETE RESTRICT;

CREATE INDEX IF NOT EXISTS invitation_exchange_sessions_person
    ON app.invitation_exchange_sessions (person_id)
    WHERE person_id IS NOT NULL;

CREATE OR REPLACE FUNCTION app.protect_invitation_exchange_binding()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.invitation_id IS DISTINCT FROM OLD.invitation_id
       OR NEW.verified_subject_hash IS DISTINCT FROM OLD.verified_subject_hash
       OR NEW.session_hash IS DISTINCT FROM OLD.session_hash
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
       OR NEW.person_id IS DISTINCT FROM OLD.person_id THEN
        RAISE EXCEPTION 'invitation exchange binding is immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_invitation_exchange_binding
    ON app.invitation_exchange_sessions;
CREATE TRIGGER trg_invitation_exchange_binding
    BEFORE UPDATE ON app.invitation_exchange_sessions
    FOR EACH ROW EXECUTE FUNCTION app.protect_invitation_exchange_binding();

COMMIT;