-- Story 38.10: enforce the shared attempt range without rewriting migration 186.
BEGIN;

CREATE OR REPLACE FUNCTION app.enforce_inbound_scan_attempt_range()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $body$
BEGIN
    IF NEW.max_attempts NOT BETWEEN 5 AND 20 THEN
        RAISE EXCEPTION 'inbound scan max_attempts must be between 5 and 20'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$body$;

DROP TRIGGER IF EXISTS trg_inbound_scan_attempt_range
    ON app.inbound_scan_jobs;
CREATE TRIGGER trg_inbound_scan_attempt_range
    BEFORE INSERT ON app.inbound_scan_jobs
    FOR EACH ROW EXECUTE FUNCTION app.enforce_inbound_scan_attempt_range();

COMMIT;