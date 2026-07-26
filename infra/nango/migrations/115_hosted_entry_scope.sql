-- Story 43.8: one accepted hosted ENTRY invitation may create one tenant scope.
--
-- Invitation acceptance remains owned by app.invitations and
-- app.invitation_exchange_sessions. This table is only the durable, immutable
-- consumption receipt linking that accepted entitlement to the scope created
-- through app.operations.

BEGIN;

CREATE TABLE IF NOT EXISTS app.hosted_entry_scope_consumptions (
    id            TEXT        PRIMARY KEY,
    person_id     TEXT        NOT NULL UNIQUE
                              REFERENCES app.persons(id) ON DELETE RESTRICT,
    invitation_id TEXT        NOT NULL UNIQUE
                              REFERENCES app.invitations(id) ON DELETE RESTRICT,
    org_id        TEXT        NOT NULL UNIQUE
                              REFERENCES app.organizations(id) ON DELETE RESTRICT
                              DEFERRABLE INITIALLY DEFERRED,
    project_id    TEXT        NOT NULL UNIQUE
                              REFERENCES app.projects(id) ON DELETE RESTRICT
                              DEFERRABLE INITIALLY DEFERRED,
    operation_id  TEXT        NOT NULL UNIQUE
                              REFERENCES app.operations(id) ON DELETE RESTRICT,
    consumed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE OR REPLACE FUNCTION app.validate_hosted_entry_scope_consumption()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM app.invitations AS invitation
        JOIN app.invitation_exchange_sessions AS exchange
          ON exchange.invitation_id = invitation.id
        WHERE invitation.id = NEW.invitation_id
          AND invitation.org_id IS NULL
          AND invitation.state = 'accepted'
          AND invitation.accepted_at IS NOT NULL
          AND invitation.superseded_by IS NULL
          AND exchange.person_id = NEW.person_id
          AND exchange.consumed_at IS NOT NULL
          AND exchange.accepted_operation_id IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'hosted ENTRY entitlement unavailable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_validate_hosted_entry_scope_consumption
    ON app.hosted_entry_scope_consumptions;
CREATE TRIGGER trg_validate_hosted_entry_scope_consumption
    BEFORE INSERT ON app.hosted_entry_scope_consumptions
    FOR EACH ROW EXECUTE FUNCTION app.validate_hosted_entry_scope_consumption();

CREATE OR REPLACE FUNCTION app.protect_hosted_entry_scope_consumption()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'hosted ENTRY consumption is immutable';
END;
$$;

DROP TRIGGER IF EXISTS trg_hosted_entry_scope_consumption_immutable
    ON app.hosted_entry_scope_consumptions;
CREATE TRIGGER trg_hosted_entry_scope_consumption_immutable
    BEFORE UPDATE OR DELETE ON app.hosted_entry_scope_consumptions
    FOR EACH ROW EXECUTE FUNCTION app.protect_hosted_entry_scope_consumption();

COMMIT;
