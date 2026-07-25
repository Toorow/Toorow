-- 098_rgpd_org_erasure_hatch.sql
--
-- Make the human-gated org drop (DELETE /api/organizations/{id}) actually
-- possible without weakening any guarantee.
--
-- Two blockers were found by running the real deletion:
--
-- 1. Append-only triggers block DELETE, not just UPDATE. `org_plan_history` is
--    ON DELETE CASCADE from `app.organizations`, so the cascade fired a DELETE
--    that its own trigger refused: dropping an org was STRUCTURALLY impossible,
--    independently of any application code. Same for the other append-only
--    tables sitting inside the org tree.
--
--    Fix: the escape-hatch idiom ALREADY used by `protect_first_value_events`
--    (DELETE allowed only inside a purge that flags itself). Here the flag is
--    `app.rgpd_erasure`, set with SET LOCAL by core/org_purge.py, so it is
--    transaction-scoped and cannot leak to another statement or session.
--    UPDATE stays blocked unconditionally: append-only means history is not
--    REWRITABLE; erasure of a whole tenant is a different, audited operation.
--
-- 2. `app.audit_log` is deliberately NOT given that hatch -- it is the durable
--    trace OF the erasure. But its FK to `app.connection_ref` pinned the org's
--    connections in place, and the FK could not even be NULLed (append-only).
--    Fix: drop the FK. An append-only ledger must not be arrimed to mutable
--    operational rows; `audit_log.connection_ref` keeps its value as historical
--    evidence of what the connection was, which is exactly what an audit trail
--    is for.
--
-- Idempotent: safe to re-run.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. audit_log survives erasure -- unpin it from connection_ref.
-- ---------------------------------------------------------------------------
ALTER TABLE app.audit_log
    DROP CONSTRAINT IF EXISTS audit_log_connection_ref_fkey;

COMMENT ON COLUMN app.audit_log.connection_ref IS
    'Historical connection identifier. Intentionally NOT a foreign key (mig 098): '
    'the audit log is append-only and must outlive the rows it describes, '
    'including after an RGPD tenant erasure.';

-- ---------------------------------------------------------------------------
-- 2. Append-only tables inside the org tree: allow DELETE only under the flag.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION app.org_plan_history_block_mutation()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'app.org_plan_history is append-only (Story 34.1): UPDATE blocked'
            USING ERRCODE = 'raise_exception';
    END IF;
    IF current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION 'app.org_plan_history is append-only (Story 34.1): DELETE blocked'
            USING ERRCODE = 'raise_exception';
    END IF;
    RETURN OLD;
END;
$function$;

CREATE OR REPLACE FUNCTION app.protect_mapping_publication_log()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'app.datastream_mapping_publication_log is append-only: UPDATE blocked'
            USING ERRCODE = 'raise_exception';
    END IF;
    IF current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION 'app.datastream_mapping_publication_log is append-only: DELETE blocked'
            USING ERRCODE = 'raise_exception';
    END IF;
    RETURN OLD;
END;
$function$;

CREATE OR REPLACE FUNCTION app.reject_external_bq_observation_mutation()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'app.external_bq_observations is append-only: UPDATE blocked'
            USING ERRCODE = 'raise_exception';
    END IF;
    IF current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION 'app.external_bq_observations is append-only: DELETE blocked'
            USING ERRCODE = 'raise_exception';
    END IF;
    RETURN OLD;
END;
$function$;

CREATE OR REPLACE FUNCTION app.metric_semantics_audit_block_mutation()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'app.metric_semantics_audit is append-only: UPDATE blocked'
            USING ERRCODE = 'raise_exception';
    END IF;
    IF current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION 'app.metric_semantics_audit is append-only: DELETE blocked'
            USING ERRCODE = 'raise_exception';
    END IF;
    RETURN OLD;
END;
$function$;

CREATE OR REPLACE FUNCTION app.protect_support_access_ledger()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'app.support_access_ledger is append-only: UPDATE blocked'
            USING ERRCODE = 'raise_exception';
    END IF;
    IF current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION 'app.support_access_ledger is append-only: DELETE blocked'
            USING ERRCODE = 'raise_exception';
    END IF;
    RETURN OLD;
END;
$function$;

-- first_value_events already has the idiom for the retention purge; the RGPD
-- erasure is a second legitimate caller. Both flags are accepted, neither is
-- implied by the other.
CREATE OR REPLACE FUNCTION app.protect_first_value_events()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'first_value_events is append-only (no UPDATE)';
    END IF;
    IF current_setting('app.funnel_purge', true) IS DISTINCT FROM 'on'
       AND current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION 'first_value_events rows are removable only by the retention purge or an RGPD erasure';
    END IF;
    RETURN OLD;
END;
$function$;

COMMIT;
