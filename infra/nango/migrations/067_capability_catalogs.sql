-- Story 36.11: MCP capability contexts and independently versioned catalogs.
-- Additive and replayable. Host/workspace identifiers remain opaque data.
--
-- One row binds an authenticated MCP endpoint to an organization, an opaque
-- host/workspace, the profiles that endpoint may expose beyond Insights, and the
-- IMMUTABLE policy/catalog versions in force when it was approved (AC3). High-risk
-- profiles (operations/governance/support) stay unavailable until a server-
-- verifiable workspace_evidence_hash is present: where cryptographic host/workspace
-- proof is not yet integrated with a real host, workspace_evidence_hash is NULL and
-- the capability service (server/core/mcp_profiles.py) fails closed to Insights.

BEGIN;

CREATE TABLE IF NOT EXISTS app.mcp_capability_contexts (
    id                      TEXT PRIMARY KEY,
    org_id                  TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    host                    TEXT,
    workspace_id            TEXT,
    workspace_type          TEXT,
    client_id               TEXT,
    -- The dedicated endpoint this context is bound to (E36-NFR04): the read
    -- connection and the opt-in administrative connection are distinct endpoints.
    endpoint_binding        TEXT NOT NULL,
    -- Profiles this endpoint may expose. Insights is always implied; higher
    -- profiles are policy opt-in and only honoured with workspace evidence.
    enabled_profiles        JSONB NOT NULL DEFAULT '["insights"]'::jsonb
                            CHECK (jsonb_typeof(enabled_profiles) = 'array'),
    -- Cryptographically verifiable client/workspace proof (64-hex sha256) or NULL.
    -- NULL => high-risk profiles unavailable (fail closed), never host self-report.
    workspace_evidence_hash TEXT CHECK (
                                workspace_evidence_hash IS NULL
                                OR workspace_evidence_hash ~ '^[0-9a-f]{64}$'
                            ),
    -- Immutable versions captured at approval time (AC3). policy_version pins the
    -- governing policy; catalog_version pins the tool-catalog snapshot the host saw.
    policy_version          TEXT NOT NULL,
    catalog_version         TEXT NOT NULL,
    immutable               BOOLEAN NOT NULL DEFAULT TRUE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS mcp_capability_contexts_org_endpoint
    ON app.mcp_capability_contexts (org_id, endpoint_binding);

CREATE INDEX IF NOT EXISTS mcp_capability_contexts_workspace
    ON app.mcp_capability_contexts (host, workspace_id)
    WHERE workspace_id IS NOT NULL;

-- The approved policy/catalog binding is immutable once created (AC3): catalog
-- evolution creates a NEW context row and republishes, it never mutates the pinned
-- versions or the workspace evidence of an existing binding in place.
CREATE OR REPLACE FUNCTION app.protect_mcp_capability_context()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.immutable
       AND (
           NEW.org_id IS DISTINCT FROM OLD.org_id
           OR NEW.endpoint_binding IS DISTINCT FROM OLD.endpoint_binding
           OR NEW.enabled_profiles IS DISTINCT FROM OLD.enabled_profiles
           OR NEW.workspace_evidence_hash IS DISTINCT FROM OLD.workspace_evidence_hash
           OR NEW.policy_version IS DISTINCT FROM OLD.policy_version
           OR NEW.catalog_version IS DISTINCT FROM OLD.catalog_version
       ) THEN
        RAISE EXCEPTION 'mcp capability context binding is immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_mcp_capability_context_immutable ON app.mcp_capability_contexts;
CREATE TRIGGER trg_mcp_capability_context_immutable
    BEFORE UPDATE ON app.mcp_capability_contexts
    FOR EACH ROW EXECUTE FUNCTION app.protect_mcp_capability_context();

COMMIT;
