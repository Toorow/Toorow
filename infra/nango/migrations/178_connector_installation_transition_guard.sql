-- Story 38.2 corrective guard: installation lifecycle writes are operation-backed.
--
-- Migration 082 froze identity but allowed direct, unaudited state changes and
-- nullable operation references. This additive replacement keeps legacy rows
-- readable while requiring every future insert/lifecycle mutation to bind a
-- canonical platform-scope operation and follow the six-state transition graph.
--
-- Schema-Change-Checklist:
--   [x] Additive and replayable (CREATE OR REPLACE + trigger recreation)
--   [x] No populated column is rewritten
--   [x] Legacy NULL operation_id rows remain readable
--   [x] New writes are source-agnostic and operation-backed

BEGIN;

CREATE OR REPLACE FUNCTION app.protect_connector_installation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    lifecycle_changed BOOLEAN;
    operation_command TEXT;
    operation_org TEXT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'connector_installations is append-only (no DELETE)'
            USING ERRCODE = '23000';
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'DOMAIN_PENDING' OR NEW.operation_id IS NULL THEN
            RAISE EXCEPTION
                'connector installation inserts require DOMAIN_PENDING and an operation'
                USING ERRCODE = '23000';
        END IF;

        SELECT command_type, effective_org_id
          INTO operation_command, operation_org
          FROM app.operations
         WHERE id = NEW.operation_id;

        IF operation_command IS DISTINCT FROM 'connector.install.applied'
           OR operation_org IS NOT NULL
        THEN
            RAISE EXCEPTION
                'connector installation insert requires a platform install operation'
                USING ERRCODE = '23000';
        END IF;

        IF NEW.responsible_actor IS NULL
           OR NEW.responsible_actor NOT IN (
            'automated', 'platform_admin', 'platform_support'
        )
           OR NEW.blocking_cause IS DISTINCT FROM 'domain_configuration_pending'
           OR NEW.last_verified_at IS NOT NULL
        THEN
            RAISE EXCEPTION
                'connector installation insert metadata is not a safe classification'
                USING ERRCODE = '23000';
        END IF;

        RETURN NEW;
    END IF;

    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.environment IS DISTINCT FROM OLD.environment
       OR NEW.connector_name IS DISTINCT FROM OLD.connector_name
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'connector_installations identity columns are immutable'
            USING ERRCODE = '23000';
    END IF;

    lifecycle_changed :=
        NEW.state IS DISTINCT FROM OLD.state
        OR NEW.blocking_cause IS DISTINCT FROM OLD.blocking_cause
        OR NEW.responsible_actor IS DISTINCT FROM OLD.responsible_actor
        OR NEW.last_verified_at IS DISTINCT FROM OLD.last_verified_at;

    IF lifecycle_changed THEN
        IF NEW.operation_id IS NULL
           OR NEW.operation_id IS NOT DISTINCT FROM OLD.operation_id
        THEN
            RAISE EXCEPTION
                'connector installation lifecycle changes require a fresh operation'
                USING ERRCODE = '23000';
        END IF;

        SELECT command_type, effective_org_id
          INTO operation_command, operation_org
          FROM app.operations
         WHERE id = NEW.operation_id;

        IF operation_command IS DISTINCT FROM 'connector.state.changed'
           OR operation_org IS NOT NULL
        THEN
            RAISE EXCEPTION
                'connector installation transition requires a platform state operation'
                USING ERRCODE = '23000';
        END IF;

        IF NEW.state IS DISTINCT FROM OLD.state AND NOT (
            (OLD.state = 'NOT_INSTALLED' AND NEW.state IN ('DOMAIN_PENDING', 'DISABLED'))
            OR (OLD.state = 'DOMAIN_PENDING' AND NEW.state IN ('VERIFYING', 'DISABLED'))
            OR (OLD.state = 'VERIFYING' AND NEW.state IN ('READY', 'DEGRADED', 'DISABLED'))
            OR (OLD.state = 'READY' AND NEW.state IN ('DEGRADED', 'DISABLED'))
            OR (OLD.state = 'DEGRADED' AND NEW.state IN ('READY', 'VERIFYING', 'DISABLED'))
        ) THEN
            RAISE EXCEPTION 'illegal connector installation state transition'
                USING ERRCODE = '23000';
        END IF;

        IF NEW.responsible_actor IS NULL
           OR NEW.responsible_actor NOT IN (
            'automated', 'platform_admin', 'platform_support'
        )
           OR (
               NEW.blocking_cause IS NOT NULL
               AND NEW.blocking_cause NOT IN (
                   'connector_disabled',
                   'dependency_unavailable',
                   'domain_configuration_pending',
                   'domain_route_misconfigured',
                   'installation_not_applied',
                   'synthetic_verification_failed',
                   'verification_failed'
               )
           )
           OR (NEW.state = 'READY' AND (
               NEW.blocking_cause IS NOT NULL OR NEW.last_verified_at IS NULL
           ))
           OR (NEW.state = 'DEGRADED' AND NEW.blocking_cause IS NULL)
        THEN
            RAISE EXCEPTION
                'connector installation transition metadata is inconsistent'
                USING ERRCODE = '23000';
        END IF;
    ELSIF NEW.operation_id IS DISTINCT FROM OLD.operation_id THEN
        RAISE EXCEPTION
            'connector installation operation cannot change without lifecycle evidence'
            USING ERRCODE = '23000';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_connector_installation_protect
    ON app.connector_installations;
CREATE TRIGGER trg_connector_installation_protect
    BEFORE INSERT OR UPDATE OR DELETE ON app.connector_installations
    FOR EACH ROW EXECUTE FUNCTION app.protect_connector_installation();

COMMENT ON FUNCTION app.protect_connector_installation() IS
    'Story 38.2 corrective guard: immutable identity, legal six-state transitions, '
    'closed safe metadata, and a fresh canonical platform operation for every '
    'installation lifecycle write.';

COMMIT;
