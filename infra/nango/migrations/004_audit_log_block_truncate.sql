-- 004: block TRUNCATE on app.audit_log (review-2-6 F-02).
--
-- Row-level triggers (migration 003) do not fire on TRUNCATE, and the owner
-- role retains the TRUNCATE privilege regardless of REVOKE. A statement-level
-- BEFORE TRUNCATE trigger closes the last mutation path short of superuser.

BEGIN;

CREATE OR REPLACE FUNCTION app.audit_log_block_truncate()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'app.audit_log is append-only (FR12): TRUNCATE blocked'
        USING ERRCODE = 'raise_exception';
END;
$$;

DROP TRIGGER IF EXISTS audit_log_block_truncate ON app.audit_log;

CREATE TRIGGER audit_log_block_truncate
    BEFORE TRUNCATE ON app.audit_log
    FOR EACH STATEMENT
    EXECUTE FUNCTION app.audit_log_block_truncate();

COMMIT;
