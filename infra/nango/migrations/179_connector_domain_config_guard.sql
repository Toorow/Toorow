-- Story 38.3 corrective guard: operation-backed, serialized domain versions.
--
-- Migration 084 established append-only rows and active uniqueness. This additive
-- guard closes the remaining write boundary: future rows and supersessions must
-- belong to the locked DOMAIN_PENDING installation, use monotonic versions, and
-- reference a canonical nullable-scope platform operation.

BEGIN;

CREATE OR REPLACE FUNCTION app.protect_connector_domain_config()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'connector_domain_configs is append-only (no DELETE)'
            USING ERRCODE = '23000';
    END IF;

    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.installation_id IS DISTINCT FROM OLD.installation_id
       OR NEW.environment IS DISTINCT FROM OLD.environment
       OR NEW.connector_name IS DISTINCT FROM OLD.connector_name
       OR NEW.domain IS DISTINCT FROM OLD.domain
       OR NEW.provider_adapter IS DISTINCT FROM OLD.provider_adapter
       OR NEW.webhook_endpoint_version IS DISTINCT FROM OLD.webhook_endpoint_version
       OR NEW.signing_secret_ref IS DISTINCT FROM OLD.signing_secret_ref
       OR NEW.dns_evidence_class IS DISTINCT FROM OLD.dns_evidence_class
       OR NEW.dns_evidence_hash IS DISTINCT FROM OLD.dns_evidence_hash
       OR NEW.config_version IS DISTINCT FROM OLD.config_version
       OR NEW.operation_id IS DISTINCT FROM OLD.operation_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'connector_domain_configs identity/evidence columns are immutable'
            USING ERRCODE = '23000';
    END IF;

    IF OLD.superseded_by IS NOT NULL
       OR NEW.superseded_by IS NULL
       OR NEW.superseded_by = NEW.id
    THEN
        RAISE EXCEPTION 'connector domain supersession must be one-way to a new row'
            USING ERRCODE = '23000';
    END IF;

    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION app.validate_connector_domain_config_write()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    operation_command TEXT;
    operation_org TEXT;
    installation_environment TEXT;
    installation_connector TEXT;
    installation_state TEXT;
    predecessor_count INT;
    replacement_count INT;
BEGIN
    SELECT command_type, effective_org_id
      INTO operation_command, operation_org
      FROM app.operations
     WHERE id = NEW.operation_id;

    IF operation_command IS DISTINCT FROM 'connector.domain.configured'
       OR operation_org IS NOT NULL
    THEN
        RAISE EXCEPTION 'connector domain config requires a platform domain operation'
            USING ERRCODE = '23000';
    END IF;

    SELECT environment, connector_name, state
      INTO installation_environment, installation_connector, installation_state
      FROM app.connector_installations
     WHERE id = NEW.installation_id;

    IF installation_environment IS DISTINCT FROM NEW.environment
       OR installation_connector IS DISTINCT FROM NEW.connector_name
       OR installation_state IS DISTINCT FROM 'DOMAIN_PENDING'
    THEN
        RAISE EXCEPTION 'connector domain config does not match a configurable installation'
            USING ERRCODE = '23000';
    END IF;

    IF NEW.signing_secret_ref IS NOT NULL
       AND NEW.signing_secret_ref !~ '^(secret-ref-[A-Za-z0-9_-]{1,200}|projects/[a-z0-9][a-z0-9-]{4,62}/secrets/[A-Za-z0-9_-]{1,255}/versions/([1-9][0-9]*|latest))$'
    THEN
        RAISE EXCEPTION 'connector domain signing secret is not a version reference'
            USING ERRCODE = '23000';
    END IF;

    IF (NEW.dns_evidence_class IS NOT NULL
        AND NEW.dns_evidence_class !~ '^[a-z][a-z0-9_]{0,63}$')
       OR (NEW.dns_evidence_hash IS NOT NULL
           AND NEW.dns_evidence_hash !~ '^[0-9a-f]{64}$')
    THEN
        RAISE EXCEPTION 'connector domain evidence classification is invalid'
            USING ERRCODE = '23000';
    END IF;

    SELECT COUNT(*)
      INTO predecessor_count
      FROM app.connector_domain_configs previous
     WHERE previous.superseded_by = NEW.id
       AND previous.installation_id = NEW.installation_id
       AND previous.environment = NEW.environment
       AND previous.connector_name = NEW.connector_name
       AND previous.config_version = NEW.config_version - 1;

    IF (NEW.config_version = 1 AND predecessor_count <> 0)
       OR (NEW.config_version > 1 AND predecessor_count <> 1)
    THEN
        RAISE EXCEPTION 'connector domain config version chain is not monotonic'
            USING ERRCODE = '23000';
    END IF;

    IF NEW.superseded_by IS NOT NULL THEN
        SELECT COUNT(*)
          INTO replacement_count
          FROM app.connector_domain_configs replacement
         WHERE replacement.id = NEW.superseded_by
           AND replacement.installation_id = NEW.installation_id
           AND replacement.environment = NEW.environment
           AND replacement.connector_name = NEW.connector_name
           AND replacement.config_version = NEW.config_version + 1;
        IF replacement_count <> 1 THEN
            RAISE EXCEPTION 'connector domain supersession target is invalid'
                USING ERRCODE = '23000';
        END IF;
    END IF;

    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_connector_domain_config_protect
    ON app.connector_domain_configs;
CREATE TRIGGER trg_connector_domain_config_protect
    BEFORE UPDATE OR DELETE ON app.connector_domain_configs
    FOR EACH ROW EXECUTE FUNCTION app.protect_connector_domain_config();

DROP TRIGGER IF EXISTS trg_connector_domain_config_validate_write
    ON app.connector_domain_configs;
CREATE CONSTRAINT TRIGGER trg_connector_domain_config_validate_write
    AFTER INSERT OR UPDATE OF superseded_by ON app.connector_domain_configs
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION app.validate_connector_domain_config_write();

COMMENT ON FUNCTION app.validate_connector_domain_config_write() IS
    'Story 38.3 corrective guard: nullable-scope operation, DOMAIN_PENDING identity, '
    'safe evidence references, and a monotonic predecessor chain.';

COMMIT;
