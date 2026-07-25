-- Story 36.5: serialize invitation lifecycle transitions and preserve accepted records.
BEGIN;

CREATE OR REPLACE FUNCTION app.enforce_invitation_lifecycle()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.state = 'accepted' AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'accepted invitation is immutable';
    END IF;

    IF NEW.state IS DISTINCT FROM OLD.state AND NOT (
        (OLD.state = 'pending' AND NEW.state IN ('delivered', 'delivery_failed', 'accepted', 'expired', 'revoked'))
        OR (OLD.state = 'delivered' AND NEW.state IN ('delivery_failed', 'accepted', 'expired', 'revoked'))
        OR (OLD.state = 'delivery_failed' AND NEW.state IN ('expired', 'revoked'))
    ) THEN
        RAISE EXCEPTION 'invalid invitation lifecycle transition: % -> %', OLD.state, NEW.state;
    END IF;

    IF NEW.superseded_by IS DISTINCT FROM OLD.superseded_by
       AND (OLD.state NOT IN ('pending', 'delivered', 'delivery_failed')
            OR NEW.state <> 'revoked'
            OR NEW.superseded_by IS NULL) THEN
        RAISE EXCEPTION 'superseded_by requires an unaccepted invitation revoked by replacement';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_invitations_lifecycle ON app.invitations;
CREATE TRIGGER trg_invitations_lifecycle
    BEFORE UPDATE ON app.invitations
    FOR EACH ROW EXECUTE FUNCTION app.enforce_invitation_lifecycle();

COMMIT;
