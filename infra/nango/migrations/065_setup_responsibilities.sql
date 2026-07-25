-- Story 36.6: durable setup responsibility checklist and minimum handoffs.
BEGIN;

CREATE TABLE IF NOT EXISTS app.setup_journeys (
    id                  TEXT PRIMARY KEY,
    org_id              TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    project_id          TEXT REFERENCES app.projects(id) ON DELETE RESTRICT,
    invitation_id       TEXT REFERENCES app.invitations(id) ON DELETE RESTRICT,
    operator_identity   TEXT NOT NULL,
    state               TEXT NOT NULL DEFAULT 'active'
                        CHECK (state IN ('active', 'verified', 'abandoned')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    verified_at         TIMESTAMPTZ,
    CONSTRAINT uq_setup_journey_invitation UNIQUE (invitation_id)
);

CREATE INDEX IF NOT EXISTS setup_journeys_project_active
    ON app.setup_journeys (project_id, updated_at DESC)
    WHERE state = 'active';

CREATE TABLE IF NOT EXISTS app.setup_tasks (
    id                  TEXT PRIMARY KEY,
    journey_id          TEXT NOT NULL REFERENCES app.setup_journeys(id) ON DELETE RESTRICT,
    step_key            TEXT NOT NULL,
    title               TEXT NOT NULL,
    actor_type          TEXT NOT NULL CHECK (actor_type IN (
                            'invited_operator', 'toorow_admin', 'credential_owner',
                            'source_admin', 'host_admin'
                        )),
    assigned_identity   TEXT,
    owner_label         TEXT NOT NULL,
    state               TEXT NOT NULL CHECK (state IN (
                            'ready', 'waiting', 'blocked', 'completed',
                            'failed', 'expired', 'revoked'
                        )),
    expires_at          TIMESTAMPTZ,
    handoff_method      TEXT NOT NULL CHECK (handoff_method IN ('none', 'link', 'task')),
    reminder_policy     JSONB NOT NULL DEFAULT '{"mode":"none"}'::jsonb
                        CHECK (jsonb_typeof(reminder_policy) = 'object'),
    return_condition    JSONB NOT NULL CHECK (jsonb_typeof(return_condition) = 'object'),
    return_path         TEXT NOT NULL,
    safe_scope          JSONB NOT NULL CHECK (jsonb_typeof(safe_scope) = 'object'),
    blocker             TEXT,
    version             INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMPTZ,
    CONSTRAINT uq_setup_task_step UNIQUE (journey_id, step_key)
);

CREATE INDEX IF NOT EXISTS setup_tasks_journey_order
    ON app.setup_tasks (journey_id, created_at, id);

CREATE TABLE IF NOT EXISTS app.setup_handoffs (
    id                          TEXT PRIMARY KEY,
    task_id                     TEXT NOT NULL REFERENCES app.setup_tasks(id) ON DELETE RESTRICT,
    purpose                     TEXT NOT NULL,
    actor_type                  TEXT NOT NULL CHECK (actor_type IN (
                                    'invited_operator', 'toorow_admin', 'credential_owner',
                                    'source_admin', 'host_admin'
                                )),
    assigned_identity_hash      TEXT CHECK (
                                    assigned_identity_hash IS NULL
                                    OR assigned_identity_hash ~ '^[0-9a-f]{64}$'
                                ),
    bearer_hash                 TEXT NOT NULL UNIQUE
                                CHECK (bearer_hash ~ '^[0-9a-f]{64}$'),
    safe_scope                  JSONB NOT NULL CHECK (jsonb_typeof(safe_scope) = 'object'),
    return_condition            JSONB NOT NULL CHECK (jsonb_typeof(return_condition) = 'object'),
    return_path                 TEXT NOT NULL,
    state                       TEXT NOT NULL DEFAULT 'created' CHECK (state IN (
                                    'created', 'delivered', 'completed', 'expired',
                                    'revoked', 'failed'
                                )),
    expires_at                  TIMESTAMPTZ NOT NULL,
    operation_id                TEXT NOT NULL REFERENCES app.operations(id) ON DELETE RESTRICT,
    superseded_by               TEXT REFERENCES app.setup_handoffs(id) ON DELETE RESTRICT,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at                TIMESTAMPTZ,
    revoked_at                  TIMESTAMPTZ,
    CONSTRAINT uq_setup_handoff_operation UNIQUE (operation_id)
);

CREATE INDEX IF NOT EXISTS setup_handoffs_task_living
    ON app.setup_handoffs (task_id, expires_at)
    WHERE state IN ('created', 'delivered');

CREATE TABLE IF NOT EXISTS app.setup_handoff_sessions (
    id                  TEXT PRIMARY KEY,
    handoff_id          TEXT NOT NULL REFERENCES app.setup_handoffs(id) ON DELETE RESTRICT,
    session_hash        TEXT NOT NULL UNIQUE CHECK (session_hash ~ '^[0-9a-f]{64}$'),
    expires_at          TIMESTAMPTZ NOT NULL,
    consumed_at         TIMESTAMPTZ,
    resolution          JSONB CHECK (resolution IS NULL OR jsonb_typeof(resolution) = 'object'),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE OR REPLACE FUNCTION app.protect_setup_handoff_binding()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.task_id IS DISTINCT FROM OLD.task_id
       OR NEW.purpose IS DISTINCT FROM OLD.purpose
       OR NEW.actor_type IS DISTINCT FROM OLD.actor_type
       OR NEW.assigned_identity_hash IS DISTINCT FROM OLD.assigned_identity_hash
       OR NEW.bearer_hash IS DISTINCT FROM OLD.bearer_hash
       OR NEW.safe_scope IS DISTINCT FROM OLD.safe_scope
       OR NEW.return_condition IS DISTINCT FROM OLD.return_condition
       OR NEW.return_path IS DISTINCT FROM OLD.return_path
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
       OR NEW.operation_id IS DISTINCT FROM OLD.operation_id THEN
        RAISE EXCEPTION 'setup handoff binding is immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_setup_handoff_binding_immutable ON app.setup_handoffs;
CREATE TRIGGER trg_setup_handoff_binding_immutable
    BEFORE UPDATE ON app.setup_handoffs
    FOR EACH ROW EXECUTE FUNCTION app.protect_setup_handoff_binding();

COMMIT;
