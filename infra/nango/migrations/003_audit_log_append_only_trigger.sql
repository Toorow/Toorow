-- 003: enforce audit_log append-only with a trigger (Story 2.6 hardening).
--
-- Why: migration 002's REVOKE UPDATE/DELETE is INEFFECTIVE for the `connector`
-- role because it OWNS the table — PostgreSQL owners implicitly retain all
-- privileges (verified: has_table_privilege returned true after the REVOKE).
-- A BEFORE trigger raises regardless of the caller's role, including the owner.
-- (A dedicated non-owner app role remains the right prod posture — see
-- infra/nango/README.md — but the trigger guarantees the invariant everywhere.)

BEGIN;

CREATE OR REPLACE FUNCTION app.audit_log_block_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'app.audit_log is append-only (FR12): % blocked', TG_OP
        USING ERRCODE = 'raise_exception';
END;
$$;

DROP TRIGGER IF EXISTS audit_log_append_only ON app.audit_log;

CREATE TRIGGER audit_log_append_only
    BEFORE UPDATE OR DELETE ON app.audit_log
    FOR EACH ROW
    EXECUTE FUNCTION app.audit_log_block_mutation();

COMMIT;
