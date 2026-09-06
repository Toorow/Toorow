-- Story 38.7 closure: durable lifecycle, provenance, quota and tenant guards.
BEGIN;

CREATE TABLE IF NOT EXISTS app.datastream_inbound_credential_rate_events (
    id              TEXT        PRIMARY KEY
                    CHECK (id ~ '^dcre_[0-9A-HJKMNP-TV-Z]{26}$'),
    operation_id    TEXT
                    REFERENCES app.operations(id) ON DELETE RESTRICT,
    environment     TEXT        NOT NULL CHECK (btrim(environment) <> ''),
    connector_name  TEXT        NOT NULL CHECK (btrim(connector_name) <> ''),
    datastream_id   TEXT
                    REFERENCES app.datastreams(id) ON DELETE RESTRICT,
    channel         TEXT        NOT NULL
                    CHECK (channel IN ('email', 'webhook', '__unknown__')),
    operation       TEXT        NOT NULL
                    CHECK (operation IN ('issue', 'rotate', 'resolve')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_dic_rate_operation
    ON app.datastream_inbound_credential_rate_events (operation_id)
    WHERE operation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_dic_rate_environment
    ON app.datastream_inbound_credential_rate_events
       (environment, operation, created_at);
CREATE INDEX IF NOT EXISTS idx_dic_rate_connector
    ON app.datastream_inbound_credential_rate_events
       (environment, connector_name, operation, created_at);
CREATE INDEX IF NOT EXISTS idx_dic_rate_capability
    ON app.datastream_inbound_credential_rate_events
       (environment, connector_name, datastream_id, channel, operation, created_at);

-- Migration-owned repairs run before the stronger trigger is installed. Version
-- numbers are reassigned by immutable creation order, preserving every row while
-- making re-issue and rotation monotonic across terminal history.
DROP TRIGGER IF EXISTS trg_inbound_credential_protect
    ON app.datastream_inbound_credentials;

WITH numbered AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY datastream_id, channel
               ORDER BY created_at, id
           ) AS repaired_version
    FROM app.datastream_inbound_credentials
)
UPDATE app.datastream_inbound_credentials c
SET version = n.repaired_version, updated_at = NOW()
FROM numbered n
WHERE c.id = n.id AND c.version IS DISTINCT FROM n.repaired_version;

-- Every migration-time lifecycle repair gets a truthful operation, audit event
-- and outbox event. No repaired ROTATING row silently changes state.
DO $repair$
DECLARE
    target RECORD;
    repair_operation_id TEXT;
    repair_audit_id TEXT;
    repair_outbox_id TEXT;
    request_digest TEXT;
    before_digest TEXT;
    after_digest TEXT;
    resource JSONB;
    result_payload JSONB;
BEGIN
    FOR target IN
        WITH ranked AS (
            SELECT c.*, d.org_id,
                   row_number() OVER (
                       PARTITION BY c.datastream_id, c.channel
                       ORDER BY c.version DESC, c.created_at DESC, c.id DESC
                   ) AS ordinal
            FROM app.datastream_inbound_credentials c
            JOIN app.datastreams d ON d.id = c.datastream_id
            WHERE c.state = 'ROTATING'
        )
        SELECT * FROM ranked
        WHERE ordinal > 1 OR overlap_until IS NULL OR overlap_until <= NOW()
        ORDER BY datastream_id, channel, version
    LOOP
        repair_operation_id := 'op_migration_' || substr(md5(target.id), 1, 26);
        repair_audit_id := 'audit_migration_' || substr(md5(target.id), 1, 26);
        repair_outbox_id := 'outbox_migration_' || substr(md5(target.id), 1, 26);
        request_digest := md5('repair-request:' || target.id)
                          || md5('repair-request-2:' || target.id);
        before_digest := md5('ROTATING:' || target.id)
                         || md5('ROTATING-2:' || target.id);
        after_digest := md5('EXPIRED:' || target.id)
                        || md5('EXPIRED-2:' || target.id);
        resource := jsonb_build_array(
            'organization:' || target.org_id,
            'datastream:' || target.datastream_id,
            'credential:' || target.id
        );
        result_payload := jsonb_build_object(
            'credential_id', target.id,
            'datastream_id', target.datastream_id,
            'channel', target.channel,
            'state', 'EXPIRED',
            'version', target.version,
            'repair', 'migration-183'
        );

        INSERT INTO app.operations (
            id, effective_org_id, command_type, actor, resource_path,
            host_context, versions, request_hash, provider_references,
            confirmation_mode, confirmation_reference_hash,
            idempotency_key_hash, state, outcome, before_hash, after_hash,
            result, audit_event_id, outbox_event_id, completed_at
        ) VALUES (
            repair_operation_id, target.org_id, 'inbound.credential.expired',
            'system:migration-183', resource, '{}'::jsonb,
            '{"policy":"inbound-credential-v2","tool":"migration-183"}'::jsonb,
            request_digest, '{}'::jsonb, 'server',
            md5('confirm:' || target.id) || md5('confirm-2:' || target.id),
            md5('idempotency:' || target.id) || md5('idempotency-2:' || target.id),
            'succeeded', 'succeeded', before_digest, after_digest,
            result_payload, repair_audit_id, repair_outbox_id, NOW()
        ) ON CONFLICT (id) DO NOTHING;

        INSERT INTO app.audit_log (
            id, identity, action, provider_account, connection_ref, metadata,
            operation_id, effective_org_id, resource_path, host_context,
            policy_version, tool_version, before_hash, after_hash,
            confirmation_reference_hash, idempotency_key_hash, outcome
        ) VALUES (
            repair_audit_id, 'system:migration-183',
            'inbound.credential.expired', '', NULL,
            jsonb_build_object('repair', 'migration-183', 'credential_id', target.id),
            repair_operation_id, target.org_id, resource, '{}'::jsonb,
            'inbound-credential-v2', 'migration-183', before_digest, after_digest,
            md5('confirm:' || target.id) || md5('confirm-2:' || target.id),
            md5('idempotency:' || target.id) || md5('idempotency-2:' || target.id),
            'succeeded'
        ) ON CONFLICT (id) DO NOTHING;

        INSERT INTO app.operation_outbox (
            id, operation_id, event_type, payload
        ) VALUES (
            repair_outbox_id, repair_operation_id, 'inbound.credential.expired',
            result_payload || jsonb_build_object('repair', 'migration-183')
        ) ON CONFLICT (id) DO NOTHING;

        UPDATE app.datastream_inbound_credentials
        SET state = 'EXPIRED', overlap_until = NULL,
            expires_at = COALESCE(expires_at, NOW()),
            operation_id = repair_operation_id, updated_at = NOW()
        WHERE id = target.id AND state = 'ROTATING';
    END LOOP;
END;
$repair$;

DO $constraints$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_dic_datastream'
          AND conrelid = 'app.datastream_inbound_credentials'::regclass
    ) THEN
        ALTER TABLE app.datastream_inbound_credentials
            ADD CONSTRAINT fk_dic_datastream
            FOREIGN KEY (datastream_id) REFERENCES app.datastreams(id)
            ON DELETE RESTRICT NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_dic_token_hash'
          AND conrelid = 'app.datastream_inbound_credentials'::regclass
    ) THEN
        ALTER TABLE app.datastream_inbound_credentials
            ADD CONSTRAINT ck_dic_token_hash
            CHECK (token_hash ~ '^[0-9a-f]{64}$') NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_dic_safe_suffix'
          AND conrelid = 'app.datastream_inbound_credentials'::regclass
    ) THEN
        ALTER TABLE app.datastream_inbound_credentials
            ADD CONSTRAINT ck_dic_safe_suffix
            CHECK (safe_suffix IS NOT NULL AND length(safe_suffix) BETWEEN 1 AND 8)
            NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_dic_operation_required'
          AND conrelid = 'app.datastream_inbound_credentials'::regclass
    ) THEN
        ALTER TABLE app.datastream_inbound_credentials
            ADD CONSTRAINT ck_dic_operation_required
            CHECK (operation_id IS NOT NULL) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_dic_lifecycle_shape'
          AND conrelid = 'app.datastream_inbound_credentials'::regclass
    ) THEN
        ALTER TABLE app.datastream_inbound_credentials
            ADD CONSTRAINT ck_dic_lifecycle_shape CHECK (
                (state = 'ROTATING' AND overlap_until IS NOT NULL)
                OR (state <> 'ROTATING' AND overlap_until IS NULL)
            ) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_dic_expired_has_boundary'
          AND conrelid = 'app.datastream_inbound_credentials'::regclass
    ) THEN
        ALTER TABLE app.datastream_inbound_credentials
            ADD CONSTRAINT ck_dic_expired_has_boundary
            CHECK (state <> 'EXPIRED' OR expires_at IS NOT NULL) NOT VALID;
    END IF;
END;
$constraints$;

-- fk_dic_datastream reste DELIBEREMENT NOT VALID, et ce n'est pas un oubli.
--
-- Mesure du 2026-08-02, en production : la table porte UNE seule ligne, et son
-- `datastream_id` (ds_mglive0001, ACTIVE, 2026-07-25 -- un artefact de probe
-- Mailgun) designe un Datastream qui n'existe plus. `VALIDATE CONSTRAINT` levait
-- donc SQLSTATE 23503 et faisait echouer toute la migration, ce qui bloquait
-- aussi 184..194 pour tout le monde.
--
-- Le bloc $repair$ ci-dessus ne pouvait pas la voir : il fait un
-- `JOIN app.datastreams`, donc un credential dont le Datastream a disparu lui
-- est invisible par construction. C'est le trou, et il est ici nomme.
--
-- Pourquoi NOT VALID suffit a la garde : en PostgreSQL une FK NOT VALID
-- CONTRAINT TOUJOURS les INSERT et les UPDATE ; elle dispense seulement de
-- verifier les lignes deja presentes. La protection vers l'avant -- la raison
-- d'etre de cette migration -- est donc entiere. Seule la ligne heritee est
-- toleree, et elle l'est explicitement plutot qu'en silence.
--
-- Ne pas ajouter le VALIDATE ici : le faire exigerait de supprimer une ligne de
-- credential en production, ce qui n'appartient pas a une migration de garde.
-- Le VALIDATE reviendra dans une migration ulterieure, quand la ligne aura ete
-- traitee par qui la possede. Suivi en AI-139.
ALTER TABLE app.datastream_inbound_credentials
    VALIDATE CONSTRAINT ck_dic_token_hash;
ALTER TABLE app.datastream_inbound_credentials
    VALIDATE CONSTRAINT ck_dic_safe_suffix;
-- ck_dic_operation_required reste NOT VALID pour la MEME ligne heritee, et pour
-- la meme raison. Mesure du 2026-08-02 : sur les cinq CHECK de cette migration,
-- l'orpheline ds_mglive0001 n'en viole qu'UNE -- celle-ci, `operation_id IS NOT
-- NULL`. Les quatre autres (token_hash, safe_suffix, lifecycle_shape,
-- expired_has_boundary) passent sur elle et gardent donc leur VALIDATE juste
-- au-dessus et au-dessous : la garde n'est PAS relachee en bloc, elle l'est sur
-- le seul predicat que la donnee heritee contredit.
--
-- C'est coherent : cette ligne date du 2026-07-25, avant que le modele de cycle
-- de vie n'existe, donc aucune operation ne l'a jamais mintee. La contrainte
-- contraint toujours toute ecriture future. Suivi en AI-139, avec le VALIDATE.
ALTER TABLE app.datastream_inbound_credentials
    VALIDATE CONSTRAINT ck_dic_lifecycle_shape;
ALTER TABLE app.datastream_inbound_credentials
    VALIDATE CONSTRAINT ck_dic_expired_has_boundary;

CREATE UNIQUE INDEX IF NOT EXISTS uq_dic_token_hash
    ON app.datastream_inbound_credentials (token_hash);
CREATE UNIQUE INDEX IF NOT EXISTS uq_dic_version_per_datastream_channel
    ON app.datastream_inbound_credentials (datastream_id, channel, version);
CREATE UNIQUE INDEX IF NOT EXISTS uq_dic_rotating_per_datastream_channel
    ON app.datastream_inbound_credentials (datastream_id, channel)
    WHERE state = 'ROTATING';

CREATE OR REPLACE FUNCTION app.protect_inbound_credential()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, app
AS $trigger$
DECLARE
    lifecycle_command TEXT;
    operation_org TEXT;
    datastream_org TEXT;
    operation_resource JSONB;
    expected_version INTEGER;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'datastream_inbound_credentials rows may not be deleted'
            USING ERRCODE = '23000';
    END IF;

    IF TG_OP = 'UPDATE' THEN
        IF NEW.id IS DISTINCT FROM OLD.id
           OR NEW.datastream_id IS DISTINCT FROM OLD.datastream_id
           OR NEW.channel IS DISTINCT FROM OLD.channel
           OR NEW.token_hash IS DISTINCT FROM OLD.token_hash
           OR NEW.safe_suffix IS DISTINCT FROM OLD.safe_suffix
           OR NEW.issued_by IS DISTINCT FROM OLD.issued_by
           OR NEW.created_at IS DISTINCT FROM OLD.created_at
           OR NEW.version IS DISTINCT FROM OLD.version
           OR NEW.rate_limit IS DISTINCT FROM OLD.rate_limit THEN
            RAISE EXCEPTION 'datastream_inbound_credentials identity is immutable'
                USING ERRCODE = '23000';
        END IF;
        IF OLD.state IN ('REVOKED', 'EXPIRED') AND NEW IS DISTINCT FROM OLD THEN
            RAISE EXCEPTION 'terminal inbound credential rows are immutable'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.operation_id IS NOT DISTINCT FROM OLD.operation_id
           AND (
               NEW.state IS DISTINCT FROM OLD.state
               OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
               OR NEW.overlap_until IS DISTINCT FROM OLD.overlap_until
           ) THEN
            RAISE EXCEPTION 'lifecycle boundary mutation requires a new operation'
                USING ERRCODE = '23000';
        END IF;
    END IF;

    SELECT o.command_type, o.effective_org_id, o.resource_path
    INTO lifecycle_command, operation_org, operation_resource
    FROM app.operations o WHERE o.id = NEW.operation_id;
    SELECT d.org_id INTO datastream_org
    FROM app.datastreams d WHERE d.id = NEW.datastream_id;

    IF lifecycle_command IS NULL
       OR operation_org IS DISTINCT FROM datastream_org
       OR NOT operation_resource @> jsonb_build_array('datastream:' || NEW.datastream_id) THEN
        RAISE EXCEPTION 'credential operation provenance is invalid'
            USING ERRCODE = '23000';
    END IF;

    IF TG_OP = 'INSERT' THEN
        SELECT COALESCE(MAX(c.version), 0) + 1
        INTO expected_version
        FROM app.datastream_inbound_credentials c
        WHERE c.datastream_id = NEW.datastream_id
          AND c.channel = NEW.channel;

        IF NEW.version IS DISTINCT FROM expected_version THEN
            RAISE EXCEPTION 'credential version must be the next historical version'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.state <> 'ACTIVE'
           OR NOT (
               (lifecycle_command = 'inbound.credential.issued' AND NEW.version >= 1)
               OR (lifecycle_command = 'inbound.credential.rotated' AND NEW.version > 1)
           ) THEN
            RAISE EXCEPTION 'credential insert provenance is invalid'
                USING ERRCODE = '23000';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.state IS DISTINCT FROM OLD.state THEN
        IF NOT (
            (OLD.state = 'ACTIVE' AND NEW.state = 'ROTATING'
             AND lifecycle_command = 'inbound.credential.rotated')
            OR (OLD.state = 'ACTIVE' AND NEW.state = 'REVOKED'
                AND lifecycle_command IN (
                    'inbound.credential.revoked', 'inbound.credential.rotated'
                ))
            OR (OLD.state = 'ROTATING' AND NEW.state = 'REVOKED'
                AND lifecycle_command = 'inbound.credential.revoked')
            OR (OLD.state = 'ACTIVE' AND NEW.state = 'EXPIRED'
                AND lifecycle_command = 'inbound.credential.expired')
            OR (OLD.state = 'ROTATING' AND NEW.state = 'EXPIRED'
                AND lifecycle_command IN (
                    'inbound.credential.expired', 'inbound.credential.rotated'
                ))
        ) THEN
            RAISE EXCEPTION 'illegal inbound credential state transition provenance'
                USING ERRCODE = '23000';
        END IF;
    ELSIF NEW.operation_id IS DISTINCT FROM OLD.operation_id
          OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
          OR NEW.overlap_until IS DISTINCT FROM OLD.overlap_until THEN
        RAISE EXCEPTION 'operation provenance without a lifecycle transition'
            USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$trigger$;

CREATE TRIGGER trg_inbound_credential_protect
    BEFORE INSERT OR UPDATE OR DELETE ON app.datastream_inbound_credentials
    FOR EACH ROW EXECUTE FUNCTION app.protect_inbound_credential();

-- AD-5: table owners are also subject to the organization-rooted policies.
ALTER TABLE app.datastream_inbound_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.datastream_inbound_credentials FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS inbound_credentials_strict
    ON app.datastream_inbound_credentials;
CREATE POLICY inbound_credentials_strict
    ON app.datastream_inbound_credentials
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR EXISTS (
            SELECT 1 FROM app.datastreams d
            WHERE d.id = datastream_id
              AND app.epic36_has_resource_access(d.org_id, 'flux', d.id)
        )
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR EXISTS (
            SELECT 1 FROM app.datastreams d
            WHERE d.id = datastream_id
              AND app.epic36_has_resource_access(d.org_id, 'flux', d.id)
        )
    );

ALTER TABLE app.datastream_inbound_credential_rate_events
    ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.datastream_inbound_credential_rate_events
    FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS inbound_credential_rate_events_read
    ON app.datastream_inbound_credential_rate_events;
CREATE POLICY inbound_credential_rate_events_read
    ON app.datastream_inbound_credential_rate_events
    FOR SELECT
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR EXISTS (
            SELECT 1 FROM app.datastreams d
            WHERE d.id = datastream_id
              AND app.epic36_has_resource_access(d.org_id, 'flux', d.id)
        )
    );
DROP POLICY IF EXISTS inbound_credential_rate_events_write
    ON app.datastream_inbound_credential_rate_events;
CREATE POLICY inbound_credential_rate_events_write
    ON app.datastream_inbound_credential_rate_events
    FOR INSERT
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR datastream_id IS NULL
        OR EXISTS (
            SELECT 1 FROM app.datastreams d
            WHERE d.id = datastream_id
              AND app.epic36_has_resource_access(d.org_id, 'flux', d.id)
        )
    );

-- Quota accounting deliberately bypasses row filtering only through these
-- bounded aggregate/write functions. The application role cannot inspect or
-- mutate raw events directly, while environment and connector totals remain
-- correct across tenants.
CREATE OR REPLACE FUNCTION app.count_inbound_credential_rate_events(
    p_environment TEXT,
    p_connector_name TEXT,
    p_datastream_id TEXT,
    p_channel TEXT,
    p_operation TEXT
) RETURNS TABLE (
    environment_count BIGINT,
    connector_count BIGINT,
    capability_count BIGINT
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, app
SET row_security = off
AS $function$
BEGIN
    IF p_environment IS NULL OR btrim(p_environment) = ''
       OR octet_length(p_environment) > 128
       OR p_connector_name IS NULL OR btrim(p_connector_name) = ''
       OR octet_length(p_connector_name) > 255
       OR (p_datastream_id IS NOT NULL AND octet_length(p_datastream_id) > 255)
       OR p_channel NOT IN ('email', 'webhook', '__unknown__')
       OR p_operation NOT IN ('issue', 'rotate', 'resolve') THEN
        RAISE EXCEPTION 'invalid inbound credential rate-count scope'
            USING ERRCODE = '22023';
    END IF;

    RETURN QUERY
    SELECT
        COUNT(*) FILTER (WHERE e.environment = p_environment),
        COUNT(*) FILTER (
            WHERE e.environment = p_environment
              AND e.connector_name = p_connector_name
        ),
        COUNT(*) FILTER (
            WHERE e.environment = p_environment
              AND e.connector_name = p_connector_name
              AND e.datastream_id IS NOT DISTINCT FROM p_datastream_id
              AND e.channel = p_channel
        )
    FROM app.datastream_inbound_credential_rate_events e
    WHERE e.operation = p_operation
      AND e.created_at >= NOW() - INTERVAL '1 hour';
END;
$function$;

CREATE OR REPLACE FUNCTION app.record_inbound_credential_rate_event(
    p_id TEXT,
    p_operation_id TEXT,
    p_environment TEXT,
    p_connector_name TEXT,
    p_datastream_id TEXT,
    p_channel TEXT,
    p_operation TEXT
) RETURNS VOID
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, app
SET row_security = off
AS $function$
BEGIN
    IF p_id IS NULL OR p_id !~ '^dcre_[0-9A-HJKMNP-TV-Z]{26}$'
       OR (p_operation_id IS NOT NULL AND octet_length(p_operation_id) > 255)
       OR p_environment IS NULL OR btrim(p_environment) = ''
       OR octet_length(p_environment) > 128
       OR p_connector_name IS NULL OR btrim(p_connector_name) = ''
       OR octet_length(p_connector_name) > 255
       OR (p_datastream_id IS NOT NULL AND octet_length(p_datastream_id) > 255)
       OR p_channel NOT IN ('email', 'webhook', '__unknown__')
       OR p_operation NOT IN ('issue', 'rotate', 'resolve') THEN
        RAISE EXCEPTION 'invalid inbound credential rate event'
            USING ERRCODE = '22023';
    END IF;

    INSERT INTO app.datastream_inbound_credential_rate_events (
        id, operation_id, environment, connector_name, datastream_id,
        channel, operation
    ) VALUES (
        p_id, p_operation_id, p_environment, p_connector_name, p_datastream_id,
        p_channel, p_operation
    );
END;
$function$;

REVOKE ALL ON TABLE app.datastream_inbound_credential_rate_events FROM PUBLIC, connector;
REVOKE ALL ON FUNCTION app.count_inbound_credential_rate_events(
    TEXT, TEXT, TEXT, TEXT, TEXT
) FROM PUBLIC;
REVOKE ALL ON FUNCTION app.record_inbound_credential_rate_event(
    TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.count_inbound_credential_rate_events(
    TEXT, TEXT, TEXT, TEXT, TEXT
) TO connector;
GRANT EXECUTE ON FUNCTION app.record_inbound_credential_rate_event(
    TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT
) TO connector;

COMMIT;
