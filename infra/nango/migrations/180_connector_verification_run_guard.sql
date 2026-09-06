-- Story 38.4 corrective guard: verification evidence is operation-backed.
--
-- Migration 085 created the append-only ledger but allowed direct, nullable and
-- internally inconsistent evidence rows. This additive trigger leaves historical
-- rows readable and rejects every future insert that is not tied to the active
-- installation/domain contract and the canonical platform operation.
--
-- Schema-Change-Checklist:
--   [x] Additive and replayable (CREATE OR REPLACE + trigger recreation)
--   [x] No populated column is rewritten
--   [x] Legacy rows remain readable
--   [x] New evidence is source-agnostic, bounded and operation-backed

BEGIN;

CREATE OR REPLACE FUNCTION app.validate_connector_verification_run_write()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    operation_command TEXT;
    operation_org TEXT;
    installation_environment TEXT;
    installation_connector TEXT;
    config_installation TEXT;
    config_environment TEXT;
    config_connector TEXT;
    config_superseded_by TEXT;
BEGIN
    SELECT command_type, effective_org_id
      INTO operation_command, operation_org
      FROM app.operations
     WHERE id = NEW.operation_id;

    IF operation_command IS DISTINCT FROM 'connector.verification.ran'
       OR operation_org IS NOT NULL
    THEN
        RAISE EXCEPTION
            'connector verification requires a platform verification operation'
            USING ERRCODE = '23000';
    END IF;

    SELECT environment, connector_name
      INTO installation_environment, installation_connector
      FROM app.connector_installations
     WHERE id = NEW.installation_id;

    IF installation_environment IS DISTINCT FROM NEW.environment
       OR installation_connector IS DISTINCT FROM NEW.connector_name
    THEN
        RAISE EXCEPTION
            'connector verification does not match its installation'
            USING ERRCODE = '23000';
    END IF;

    SELECT installation_id, environment, connector_name, superseded_by
      INTO config_installation, config_environment, config_connector,
           config_superseded_by
      FROM app.connector_domain_configs
     WHERE id = NEW.domain_config_id;

    IF config_installation IS DISTINCT FROM NEW.installation_id
       OR config_environment IS DISTINCT FROM NEW.environment
       OR config_connector IS DISTINCT FROM NEW.connector_name
       OR config_superseded_by IS NOT NULL
    THEN
        RAISE EXCEPTION
            'connector verification requires the active matching domain config'
            USING ERRCODE = '23000';
    END IF;

    IF NEW.evidence_hash IS NULL
       OR NEW.evidence_hash !~ '^[0-9a-f]{64}$'
       OR NEW.ttl_seconds IS NULL
       OR NEW.ttl_seconds NOT BETWEEN 1 AND 86400
       OR (
           NEW.synthetic_delivery
           AND (
               NEW.evidence_class IS DISTINCT FROM 'synthetic_delivery_auth'
               OR NEW.outcome = 'degraded'
           )
       )
       OR (
           NOT NEW.synthetic_delivery
           AND NEW.evidence_class NOT IN ('routing_check', 'auth_check')
       )
    THEN
        RAISE EXCEPTION
            'connector verification evidence classification is invalid'
            USING ERRCODE = '23000';
    END IF;

    IF NEW.blocking_reason IS NOT NULL
       AND NEW.blocking_reason NOT IN (
           'connector_disabled',
           'dependency_unavailable',
           'domain_configuration_pending',
           'domain_route_misconfigured',
           'installation_not_applied',
           'synthetic_verification_failed',
           'verification_failed'
       )
    THEN
        RAISE EXCEPTION
            'connector verification blocking reason is not a safe code'
            USING ERRCODE = '23000';
    END IF;

    IF (NEW.outcome = 'passed' AND (
            NEW.blocking_reason IS NOT NULL OR NEW.first_seen_at IS NOT NULL
        ))
       OR (NEW.outcome = 'failed' AND (
            NEW.blocking_reason IS NULL OR NEW.first_seen_at IS NOT NULL
        ))
       OR (NEW.outcome = 'degraded' AND (
            NEW.blocking_reason IS NULL OR NEW.first_seen_at IS NULL
        ))
    THEN
        RAISE EXCEPTION
            'connector verification outcome metadata is inconsistent'
            USING ERRCODE = '23000';
    END IF;

    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_connector_verification_run_validate_write
    ON app.connector_verification_runs;
CREATE CONSTRAINT TRIGGER trg_connector_verification_run_validate_write
    AFTER INSERT ON app.connector_verification_runs
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION app.validate_connector_verification_run_write();

COMMENT ON FUNCTION app.validate_connector_verification_run_write() IS
    'Story 38.4 corrective guard: operation provenance, active installation/domain '
    'identity, bounded safe evidence, TTL and outcome metadata.';

COMMIT;
