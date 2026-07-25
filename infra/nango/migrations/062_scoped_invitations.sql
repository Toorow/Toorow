-- Story 36.3: immutable scoped invitations with one-way bearer binding.
-- 061 is intentionally owned by the concurrent Epic 35 daily-insights work.

BEGIN;

CREATE TABLE IF NOT EXISTS app.invitations (
    id                      TEXT        PRIMARY KEY,
    invited_identity_hash   TEXT        NOT NULL
                            CHECK (invited_identity_hash ~ '^[0-9a-f]{64}$'),
    org_id                  TEXT        NOT NULL
                            REFERENCES app.organizations(id) ON DELETE RESTRICT,
    invited_role            TEXT        NOT NULL
                            CHECK (invited_role IN ('owner', 'admin', 'member', 'viewer')),
    grant_bindings          JSONB       NOT NULL
                            CHECK (jsonb_typeof(grant_bindings) = 'array'),
    issuer                  TEXT        NOT NULL,
    policy_version          TEXT        NOT NULL,
    bearer_hash             TEXT        NOT NULL UNIQUE
                            CHECK (bearer_hash ~ '^[0-9a-f]{64}$'),
    bearer_purpose          TEXT        NOT NULL DEFAULT 'invitation_accept'
                            CHECK (bearer_purpose = 'invitation_accept'),
    bearer_version          INTEGER     NOT NULL DEFAULT 1 CHECK (bearer_version > 0),
    state                   TEXT        NOT NULL DEFAULT 'pending'
                            CHECK (state IN (
                                'pending', 'delivered', 'accepted', 'expired', 'revoked', 'delivery_failed'
                            )),
    expires_at              TIMESTAMPTZ NOT NULL,
    operation_id            TEXT        NOT NULL
                            REFERENCES app.operations(id) ON DELETE RESTRICT,
    superseded_by           TEXT        REFERENCES app.invitations(id) ON DELETE RESTRICT,
    delivery_evidence_class TEXT,
    delivery_evidence_hash  TEXT        CHECK (
                                delivery_evidence_hash IS NULL
                                OR delivery_evidence_hash ~ '^[0-9a-f]{64}$'
                            ),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    accepted_at             TIMESTAMPTZ,
    revoked_at              TIMESTAMPTZ,
    CONSTRAINT uq_invitation_operation UNIQUE (operation_id)
);

CREATE INDEX IF NOT EXISTS invitations_org_created
    ON app.invitations (org_id, created_at DESC);
CREATE INDEX IF NOT EXISTS invitations_identity_living
    ON app.invitations (invited_identity_hash, expires_at)
    WHERE state IN ('pending', 'delivered');

CREATE OR REPLACE FUNCTION app.protect_invitation_binding()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.invited_identity_hash IS DISTINCT FROM OLD.invited_identity_hash
       OR NEW.org_id IS DISTINCT FROM OLD.org_id
       OR NEW.invited_role IS DISTINCT FROM OLD.invited_role
       OR NEW.grant_bindings IS DISTINCT FROM OLD.grant_bindings
       OR NEW.issuer IS DISTINCT FROM OLD.issuer
       OR NEW.policy_version IS DISTINCT FROM OLD.policy_version THEN
        RAISE EXCEPTION 'invitation binding is immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_invitations_binding_immutable ON app.invitations;
CREATE TRIGGER trg_invitations_binding_immutable
    BEFORE UPDATE ON app.invitations
    FOR EACH ROW EXECUTE FUNCTION app.protect_invitation_binding();

COMMIT;
