-- Story 36.14: capability-driven host preflight, install handoff and binding.
-- Additive and replayable. Host identifiers remain OPAQUE data (E36-NFR03: no
-- brand-first ordering -- a host is keyed by an opaque host_key, never ordered by
-- name). NO token or secret is ever stored here: only identifiers, opaque
-- capability facts and references to the shared operation + capability-context rows.
--
-- One row is the DATED, capability-negotiated preflight for a selected host:
-- capabilities, plan constraints, required host-admin role, UI-support flag and
-- decision, catalog-refresh behaviour, and whether server-verifiable workspace
-- proof is required. On a verified install/authorization the preflight binds to a
-- Story 36.11 app.mcp_capability_contexts row (migration 067) which pins the
-- endpoint profile / org / policy / catalog version; high-risk profiles bind ONLY
-- with that verifiable workspace proof (else the 36.11 service fails closed).

BEGIN;

CREATE TABLE IF NOT EXISTS app.host_preflights (
    id                      TEXT PRIMARY KEY,
    -- Opaque host identifier (E36-NFR03). NEVER used to order/prefer hosts.
    host_key                TEXT NOT NULL,
    org_id                  TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    project_id              TEXT REFERENCES app.projects(id) ON DELETE RESTRICT,
    -- The host_connection setup task this preflight binds to (return signal).
    task_id                 TEXT NOT NULL REFERENCES app.setup_tasks(id) ON DELETE RESTRICT,
    -- Dated, capability-negotiated facts recorded at selection time (AC1).
    capabilities            JSONB NOT NULL CHECK (jsonb_typeof(capabilities) = 'array'),
    plan_constraints        JSONB NOT NULL DEFAULT '{}'::jsonb
                            CHECK (jsonb_typeof(plan_constraints) = 'object'),
    required_role           TEXT NOT NULL,
    -- app UI support flag + the resolved UI-support decision branch.
    ui_supported            BOOLEAN NOT NULL,
    ui_support_mode         TEXT NOT NULL CHECK (
                                ui_support_mode IN ('app_ui', 'standard_tool_fallback')
                            ),
    catalog_refresh         JSONB NOT NULL DEFAULT '{}'::jsonb
                            CHECK (jsonb_typeof(catalog_refresh) = 'object'),
    -- Whether server-verifiable workspace proof is required before high-risk
    -- profiles may ever bind for this host (fail closed when true and absent).
    workspace_proof_required BOOLEAN NOT NULL DEFAULT TRUE,
    dated_at                TEXT NOT NULL,
    state                   TEXT NOT NULL DEFAULT 'prepared' CHECK (state IN (
                                'prepared', 'handed_off', 'installed',
                                'bound', 'stale', 'failed'
                            )),
    -- The Story 36.11 capability context this preflight bound (migration 067).
    -- NULL until a verified bind; the binding row itself is immutable (067 trigger).
    capability_context_id   TEXT REFERENCES app.mcp_capability_contexts(id) ON DELETE RESTRICT,
    operation_id            TEXT REFERENCES app.operations(id) ON DELETE RESTRICT,
    expires_at              TIMESTAMPTZ NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_host_preflight_operation UNIQUE (operation_id)
);

CREATE INDEX IF NOT EXISTS host_preflights_task_living
    ON app.host_preflights (task_id, expires_at)
    WHERE state IN ('prepared', 'handed_off', 'installed');

CREATE INDEX IF NOT EXISTS host_preflights_org
    ON app.host_preflights (org_id, updated_at DESC);

-- The security-critical binding columns are immutable after preparation: only
-- `state`, `capability_context_id` (set once at bind) and `updated_at` may change
-- (transition machine). Mirrors 068's protect_source_delegation_binding. Once a
-- capability_context_id is bound it is immutable too (the context row itself is
-- immutable via 067's trigger); re-approval after a `stale` transition mints a NEW
-- preflight + NEW context rather than mutating this binding in place.
CREATE OR REPLACE FUNCTION app.protect_host_preflight_binding()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.host_key IS DISTINCT FROM OLD.host_key
       OR NEW.org_id IS DISTINCT FROM OLD.org_id
       OR NEW.project_id IS DISTINCT FROM OLD.project_id
       OR NEW.task_id IS DISTINCT FROM OLD.task_id
       OR NEW.capabilities IS DISTINCT FROM OLD.capabilities
       OR NEW.plan_constraints IS DISTINCT FROM OLD.plan_constraints
       OR NEW.required_role IS DISTINCT FROM OLD.required_role
       OR NEW.ui_supported IS DISTINCT FROM OLD.ui_supported
       OR NEW.ui_support_mode IS DISTINCT FROM OLD.ui_support_mode
       OR NEW.catalog_refresh IS DISTINCT FROM OLD.catalog_refresh
       OR NEW.workspace_proof_required IS DISTINCT FROM OLD.workspace_proof_required
       OR NEW.dated_at IS DISTINCT FROM OLD.dated_at
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
       OR NEW.operation_id IS DISTINCT FROM OLD.operation_id
       OR (OLD.capability_context_id IS NOT NULL
           AND NEW.capability_context_id IS DISTINCT FROM OLD.capability_context_id) THEN
        RAISE EXCEPTION 'host preflight binding is immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_host_preflight_binding_immutable ON app.host_preflights;
CREATE TRIGGER trg_host_preflight_binding_immutable
    BEFORE UPDATE ON app.host_preflights
    FOR EACH ROW EXECUTE FUNCTION app.protect_host_preflight_binding();

COMMIT;
