-- Story 38.5 corrective guard: org activation writes are operation-backed.
--
-- Migration 087 established the row and identity immutability but accepted
-- phantom organizations, nullable operations and arbitrary lifecycle updates.
-- This additive guard leaves historical rows readable while enforcing all future
-- inserts/reactivations/deactivations at the database boundary.

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'fk_connector_activations_org'
           AND conrelid = 'app.connector_activations'::regclass
    ) THEN
        ALTER TABLE app.connector_activations
            ADD CONSTRAINT fk_connector_activations_org
            FOREIGN KEY (org_id) REFERENCES app.organizations(id)
            ON DELETE RESTRICT NOT VALID;
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION app.protect_connector_activation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    operation_command TEXT;
    operation_org TEXT;
    operation_actor TEXT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'connector_activations rows may not be deleted -- deactivate instead'
            USING ERRCODE = '23000';
    END IF;

    SELECT command_type, effective_org_id, actor
      INTO operation_command, operation_org, operation_actor
      FROM app.operations
     WHERE id = NEW.operation_id;

    IF operation_org IS DISTINCT FROM NEW.org_id THEN
        RAISE EXCEPTION
            'connector activation requires an operation for the same organization'
            USING ERRCODE = '23000';
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF operation_command IS DISTINCT FROM 'connector.activation.activated'
           OR NEW.state IS DISTINCT FROM 'ACTIVE'
           OR NEW.deactivated_at IS NOT NULL
           OR NEW.activated_by IS DISTINCT FROM operation_actor
        THEN
            RAISE EXCEPTION
                'connector activation insert metadata is inconsistent'
                USING ERRCODE = '23000';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.org_id IS DISTINCT FROM OLD.org_id
       OR NEW.connector_name IS DISTINCT FROM OLD.connector_name
       OR NEW.environment IS DISTINCT FROM OLD.environment
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.activated_by IS DISTINCT FROM OLD.activated_by
       OR NEW.activated_at IS DISTINCT FROM OLD.activated_at
    THEN
        RAISE EXCEPTION 'connector activation identity columns are immutable'
            USING ERRCODE = '23000';
    END IF;

    IF NEW.operation_id IS NULL
       OR NEW.operation_id IS NOT DISTINCT FROM OLD.operation_id
    THEN
        RAISE EXCEPTION
            'connector activation lifecycle changes require a fresh operation'
            USING ERRCODE = '23000';
    END IF;

    IF NEW.state = 'ACTIVE' THEN
        IF operation_command IS DISTINCT FROM 'connector.activation.activated'
           OR NEW.deactivated_at IS NOT NULL
           OR OLD.state NOT IN ('ACTIVE', 'DEACTIVATED')
        THEN
            RAISE EXCEPTION 'connector reactivation metadata is inconsistent'
                USING ERRCODE = '23000';
        END IF;
    ELSIF NEW.state = 'DEACTIVATED' THEN
        IF operation_command IS DISTINCT FROM 'connector.activation.deactivated'
           OR NEW.deactivated_at IS NULL
           OR OLD.state IS DISTINCT FROM 'ACTIVE'
        THEN
            RAISE EXCEPTION 'connector deactivation metadata is inconsistent'
                USING ERRCODE = '23000';
        END IF;
    ELSE
        RAISE EXCEPTION 'connector activation state is invalid'
            USING ERRCODE = '23000';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_connector_activation_protect
    ON app.connector_activations;
CREATE TRIGGER trg_connector_activation_protect
    BEFORE INSERT OR UPDATE OR DELETE ON app.connector_activations
    FOR EACH ROW EXECUTE FUNCTION app.protect_connector_activation();

COMMENT ON FUNCTION app.protect_connector_activation() IS
    'Story 38.5 corrective guard: org FK, operation provenance, actor ownership, '
    'timestamp consistency and ACTIVE/DEACTIVATED lifecycle.';

COMMIT;
