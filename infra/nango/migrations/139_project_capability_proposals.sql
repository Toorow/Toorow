-- Story 48.1: one immutable, source-agnostic per-Datastream capability proposal.
--
-- Story 46.3 (migration 131) gave Project Settings its control plane: intent,
-- immutable Configuration Versions and Change Sets. What it never had is the
-- Data-owned half of the same lifecycle -- an exact, per-Datastream proposal
-- whose coverage can be counted rather than asserted. Coverage was derived from
-- a last-reference-wins read that reported every active Datastream as pending.
--
-- Three objects close that gap, all Project-scoped and immutable after
-- preparation, and all storing safe metadata only -- never credentials, raw
-- rows or unmasked values:
--
--   app.datastream_capability_proposals       the exact per-Datastream proposal
--   app.project_configuration_owner_references the complete owner set an
--                                              activated version references
--   app.capability_exceptions                  an accountable deviation
--
-- The applicable denominator is the sum of the five coverage states; a
-- not_applicable Datastream is recorded, and stays out of it. That invariant is
-- enforced in one place (core.capability_proposals) and made checkable here.

BEGIN;

-- ---------------------------------------------------------------------------
-- The per-Datastream proposal. One row per (Change Set, Datastream, capability).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.datastream_capability_proposals (
    id TEXT PRIMARY KEY CHECK (id ~ '^dscp_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    project_id TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    datastream_id TEXT NOT NULL,
    capability_key TEXT NOT NULL CHECK (capability_key IN (
        'country', 'currency_fx', 'reporting_timezone', 'tax_fees', 'competitors'
    )),
    change_set_id TEXT NOT NULL,

    -- The Project posture this proposal was compiled against, and the exact
    -- posture it intends to activate. The intended version does not exist yet at
    -- prepare time, so it is pinned by the content hash that will identify it --
    -- app.project_configuration_versions is UNIQUE (project_id, content_hash),
    -- so the pin resolves to exactly one version once activation mints it.
    base_configuration_version_id TEXT,
    intended_configuration_content_hash TEXT NOT NULL
        CHECK (intended_configuration_content_hash ~ '^[0-9a-f]{64}$'),

    -- Exact dependency pins. A change to any of these invalidates the proposal.
    connector_contract JSONB NOT NULL CHECK (jsonb_typeof(connector_contract) = 'object'),
    source_schema JSONB NOT NULL CHECK (jsonb_typeof(source_schema) = 'object'),
    current_plan_version_id TEXT,
    current_mapping_version_id TEXT,
    current_published_execution_id TEXT,
    governance_owner_references JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(governance_owner_references) = 'array'),

    applicability TEXT NOT NULL CHECK (applicability IN ('applicable', 'not_applicable')),
    coverage_state TEXT NOT NULL CHECK (coverage_state IN (
        'complete', 'partial', 'unavailable', 'excluded', 'pending', 'not_applicable'
    )),

    -- The complete impact shape the ratified Data contract requires a capability
    -- to expose per Datastream. Presence is enforced here so a compiler cannot
    -- quietly ship a proposal that answers only half the question.
    impact JSONB NOT NULL CHECK (
        jsonb_typeof(impact) = 'object'
        AND impact ? 'detected_support_selection'
        AND impact ? 'grain_before_after'
        AND impact ? 'cardinality_scan'
        AND impact ? 'quota_cost'
        AND impact ? 'recent_history'
        AND impact ? 'historical_coverage'
        AND impact ? 'backfill'
        AND impact ? 'fan_out'
    ),

    blocker_references JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(blocker_references) = 'array'),
    exception_references JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(exception_references) = 'array'),
    repair_reference JSONB CHECK (repair_reference IS NULL OR jsonb_typeof(repair_reference) = 'object'),

    dependency_snapshot JSONB NOT NULL CHECK (jsonb_typeof(dependency_snapshot) = 'object'),
    dependency_fingerprint TEXT NOT NULL CHECK (dependency_fingerprint ~ '^[0-9a-f]{64}$'),
    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),

    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (project_id, id),
    -- Exactly one proposal per Datastream and capability inside one Change Set.
    UNIQUE (project_id, change_set_id, datastream_id, capability_key),
    -- Deterministic content: the same compiled proposal never lands twice.
    UNIQUE (project_id, content_hash),
    -- Applicability and the denominator agree, or the row does not exist.
    CHECK ((applicability = 'not_applicable') = (coverage_state = 'not_applicable')),

    FOREIGN KEY (datastream_id, project_id)
        REFERENCES app.datastreams(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, change_set_id)
        REFERENCES app.project_change_sets(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, base_configuration_version_id)
        REFERENCES app.project_configuration_versions(project_id, id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_datastream_capability_proposals_change_set
    ON app.datastream_capability_proposals (project_id, change_set_id, capability_key);
CREATE INDEX IF NOT EXISTS idx_datastream_capability_proposals_datastream
    ON app.datastream_capability_proposals (project_id, datastream_id, capability_key, created_at DESC);

CREATE OR REPLACE FUNCTION app.reject_capability_proposal_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'datastream capability proposals are immutable after preparation';
END;
$$;
DROP TRIGGER IF EXISTS trg_datastream_capability_proposals_immutable
    ON app.datastream_capability_proposals;
CREATE TRIGGER trg_datastream_capability_proposals_immutable
    BEFORE UPDATE OR DELETE ON app.datastream_capability_proposals
    FOR EACH ROW EXECUTE FUNCTION app.reject_capability_proposal_mutation();

-- ---------------------------------------------------------------------------
-- The complete owner set an activated Configuration Version references.
-- Unchanged references are carried forward explicitly, so an active version
-- always names its whole Data and Governance surface -- not only what changed.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.project_configuration_owner_references (
    project_id TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    configuration_version_id TEXT NOT NULL,
    owner_kind TEXT NOT NULL CHECK (owner_kind IN ('data', 'governance')),
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    capability_key TEXT CHECK (capability_key IS NULL OR capability_key IN (
        'country', 'currency_fx', 'reporting_timezone', 'tax_fees', 'competitors'
    )),
    -- The shared semantic owner reference (surface/workspace/section/object/tab).
    -- Never a browser URL: the client alone builds hrefs from the canonical
    -- navigation registry, so a retired path cannot be frozen into a version.
    owner_reference JSONB NOT NULL CHECK (jsonb_typeof(owner_reference) = 'object'),
    evidence_hash TEXT NOT NULL CHECK (evidence_hash ~ '^[0-9a-f]{64}$'),
    proposal_id TEXT,
    carried_forward BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (project_id, configuration_version_id, owner_kind, object_type, object_id, version_id),
    FOREIGN KEY (project_id, configuration_version_id)
        REFERENCES app.project_configuration_versions(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, proposal_id)
        REFERENCES app.datastream_capability_proposals(project_id, id) ON DELETE RESTRICT
);

CREATE OR REPLACE FUNCTION app.reject_configuration_owner_reference_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'activated configuration owner references are immutable';
END;
$$;
DROP TRIGGER IF EXISTS trg_project_configuration_owner_references_immutable
    ON app.project_configuration_owner_references;
CREATE TRIGGER trg_project_configuration_owner_references_immutable
    BEFORE UPDATE OR DELETE ON app.project_configuration_owner_references
    FOR EACH ROW EXECUTE FUNCTION app.reject_configuration_owner_reference_mutation();

-- ---------------------------------------------------------------------------
-- An accountable deviation. An exclusion is a governed decision with a reason,
-- an owner and evidence -- it is never the absence of support, which is what
-- `unavailable` means.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.capability_exceptions (
    id TEXT PRIMARY KEY CHECK (id ~ '^capx_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    project_id TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    capability_key TEXT NOT NULL CHECK (capability_key IN (
        'country', 'currency_fx', 'reporting_timezone', 'tax_fees', 'competitors'
    )),
    change_set_id TEXT NOT NULL,
    datastream_id TEXT,
    proposal_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('exception', 'exclusion')),
    severity TEXT NOT NULL CHECK (severity IN ('blocking', 'degrading', 'informational')),
    reason_code TEXT NOT NULL CHECK (length(reason_code) BETWEEN 1 AND 80),
    reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 1000),
    owner_kind TEXT NOT NULL CHECK (owner_kind IN ('data', 'governance')),
    owner_reference JSONB NOT NULL CHECK (jsonb_typeof(owner_reference) = 'object'),
    repair_reference JSONB NOT NULL CHECK (jsonb_typeof(repair_reference) = 'object'),
    evidence_hash TEXT NOT NULL CHECK (evidence_hash ~ '^[0-9a-f]{64}$'),
    evidence_version_id TEXT,
    lifecycle_state TEXT NOT NULL DEFAULT 'open'
        CHECK (lifecycle_state IN ('open', 'accepted', 'resolved', 'superseded')),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (project_id, id),
    -- An exclusion removes one named Datastream from coverage; it always says which.
    CHECK (kind <> 'exclusion' OR datastream_id IS NOT NULL),
    FOREIGN KEY (project_id, change_set_id)
        REFERENCES app.project_change_sets(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (datastream_id, project_id)
        REFERENCES app.datastreams(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, proposal_id)
        REFERENCES app.datastream_capability_proposals(project_id, id) ON DELETE RESTRICT
);

-- One exception per (Change Set, capability, scope, reason): a recompiled
-- prepare reuses the stable identity instead of minting a duplicate.
CREATE UNIQUE INDEX IF NOT EXISTS uq_capability_exceptions_scope
    ON app.capability_exceptions
       (project_id, change_set_id, capability_key, reason_code, COALESCE(datastream_id, ''));

CREATE INDEX IF NOT EXISTS idx_capability_exceptions_open
    ON app.capability_exceptions (project_id, capability_key, lifecycle_state);

-- Lifecycle state advances; identity, scope and evidence never do.
CREATE OR REPLACE FUNCTION app.protect_capability_exception_identity()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'capability exceptions are never deleted; supersede them';
    END IF;
    IF NEW.org_id IS DISTINCT FROM OLD.org_id
        OR NEW.project_id IS DISTINCT FROM OLD.project_id
        OR NEW.capability_key IS DISTINCT FROM OLD.capability_key
        OR NEW.change_set_id IS DISTINCT FROM OLD.change_set_id
        OR NEW.datastream_id IS DISTINCT FROM OLD.datastream_id
        OR NEW.proposal_id IS DISTINCT FROM OLD.proposal_id
        OR NEW.kind IS DISTINCT FROM OLD.kind
        OR NEW.reason_code IS DISTINCT FROM OLD.reason_code
        OR NEW.evidence_hash IS DISTINCT FROM OLD.evidence_hash
        OR NEW.created_by IS DISTINCT FROM OLD.created_by
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'capability exception identity, scope and evidence are immutable';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_capability_exceptions_identity ON app.capability_exceptions;
CREATE TRIGGER trg_capability_exceptions_identity
    BEFORE UPDATE OR DELETE ON app.capability_exceptions
    FOR EACH ROW EXECUTE FUNCTION app.protect_capability_exception_identity();

-- ---------------------------------------------------------------------------
-- The trusted-presence binding for a confirmation.
--
-- app.entry_confirmations stores only the DIGEST of its secret, and only the
-- issuing surface ever holds the plaintext. That is correct, and it means an MCP
-- host can never present the secret -- passing it as a tool argument would put
-- authorization material straight into model-visible context, which AD-27
-- forbids outright.
--
-- So a trusted interactive surface registers, in the same transaction that
-- issues the confirmation, the server-minted presence evidence for its bound
-- endpoint and workspace. A confirmed write then authenticates on that binding:
-- the host presents no material at all, the server resolves the confirmation
-- from evidence the host cannot forge and the model never sees, and the binding
-- is single-use like the secret it stands in for.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.entry_confirmation_presence_bindings (
    confirmation_id TEXT PRIMARY KEY
        REFERENCES app.entry_confirmations(id) ON DELETE RESTRICT,
    presence_evidence_hash TEXT NOT NULL CHECK (presence_evidence_hash ~ '^[0-9a-f]{64}$'),
    endpoint_binding_hash TEXT NOT NULL CHECK (endpoint_binding_hash ~ '^[0-9a-f]{64}$'),
    workspace_evidence_hash TEXT NOT NULL CHECK (workspace_evidence_hash ~ '^[0-9a-f]{64}$'),
    payload_hash TEXT NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- At most one live binding per (presence evidence, payload): a second trusted
-- surface cannot silently attach itself to a confirmation already spoken for.
CREATE UNIQUE INDEX IF NOT EXISTS uq_entry_confirmation_presence
    ON app.entry_confirmation_presence_bindings (presence_evidence_hash, payload_hash);

CREATE OR REPLACE FUNCTION app.protect_entry_confirmation_presence_binding()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'confirmation presence bindings are immutable';
END;
$$;
DROP TRIGGER IF EXISTS trg_entry_confirmation_presence_immutable
    ON app.entry_confirmation_presence_bindings;
CREATE TRIGGER trg_entry_confirmation_presence_immutable
    BEFORE UPDATE OR DELETE ON app.entry_confirmation_presence_bindings
    FOR EACH ROW EXECUTE FUNCTION app.protect_entry_confirmation_presence_binding();

COMMIT;
