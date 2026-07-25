-- Epic 36 control-phase hardening (review of 36.3): the invitation immutability
-- trigger (062) protected identity/org/role/grants/issuer/policy but omitted the
-- set-once binding columns bearer_hash, expires_at and operation_id. Bearer
-- rotation and expiry extension are modeled as a NEW superseding invitation row
-- (resend), so these columns never legitimately mutate in place. Add them to the
-- immutable set as defense-in-depth against a stray UPDATE rebinding an existing
-- invitation's bearer/expiry. Idempotent CREATE OR REPLACE.

BEGIN;

CREATE OR REPLACE FUNCTION app.protect_invitation_binding()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.invited_identity_hash IS DISTINCT FROM OLD.invited_identity_hash
       OR NEW.org_id IS DISTINCT FROM OLD.org_id
       OR NEW.invited_role IS DISTINCT FROM OLD.invited_role
       OR NEW.grant_bindings IS DISTINCT FROM OLD.grant_bindings
       OR NEW.issuer IS DISTINCT FROM OLD.issuer
       OR NEW.policy_version IS DISTINCT FROM OLD.policy_version
       OR NEW.bearer_hash IS DISTINCT FROM OLD.bearer_hash
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
       OR NEW.operation_id IS DISTINCT FROM OLD.operation_id THEN
        RAISE EXCEPTION 'invitation binding is immutable';
    END IF;
    RETURN NEW;
END;
$$;

COMMIT;
