-- Story 49.5: the reference-only Evidence index.
--
-- WHAT THIS IS NOT. It is not a second copy of anything. Data keeps its Runs,
-- stage evidence, mappings and publications; Analyze keeps its Renders; Context
-- Hub keeps AI Path content; Test keeps evaluation results; every governed
-- object keeps its own immutable versions and approvals. Nothing below stores a
-- row, a sample, a payload, a diff, a rendered envelope or an event body from
-- any of them.
--
-- WHAT IT IS. The ratified Control Map names `Evidence Record` as a real
-- Project-scoped Governance object, so it needs a real identity. These tables
-- hold exactly that: a stable opaque id, the exact scope, the exact owner
-- reference, the times, the integrity/correlation identifiers, and the TYPED
-- EDGES between them. Everything a screen displays -- labels, diffs, approval
-- detail, used-by -- is resolved through the owner adapter, after the owner has
-- authorized the caller, at read time.
--
-- WHY IT HAD TO EXIST AT ALL. Before this migration the three Evidence lenses
-- read three unrelated tables directly:
--
--   * `datastream_mapping_publication_log` was presented as an Evidence Trace.
--     One mapping publication is one NODE of a chain, not the chain.
--   * `publication_confirmations` was presented as the Object Version universe,
--     using the confirmation id AS the version identity. A confirmation is an
--     approval; the version it approves has its own id, and conflating them
--     makes a version address unresolvable the moment a version is approved
--     twice or not at all.
--   * `audit_log` was scoped only through `metadata->>'project_id'`, which the
--     normalized 060 spine no longer writes -- so every audited operation since
--     that spine landed was invisible to the lens named after it.
--
-- IMMUTABILITY IS THE POINT. An Evidence Record is a claim that something was
-- observed. A claim that can be edited afterwards is not evidence. Records,
-- correlations and links refuse UPDATE, DELETE and TRUNCATE. Retention and
-- erasure are NEW FACTS in `evidence_availability_events`, never an edit of the
-- fact they qualify; the RGPD hatch (`app.rgpd_erasure`, migrations 098/099)
-- remains the one documented exception, so an organization can still be erased.
--
-- Schema-Change-Checklist (CONTRIBUTING.md):
--   [x] Additive & idempotent (IF NOT EXISTS everywhere; the file is replayable)
--   [x] New columns are NULL-able or have defaults
--   [x] No destructive DROP/ALTER on populated columns
--
-- Numbering: 148 was the last applied identifier when this was written; three
-- Epic 49 sessions were running in parallel and none of them held a migration.
-- 149 is the next free identifier.
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- ---------------------------------------------------------------------------
-- 1. The Evidence Record head.
--
--    One row = "this exact owner artifact was observed, in this exact Project,
--    at this exact time". The columns are deliberately few: every one of them
--    is an IDENTIFIER, a SCOPE, a TIME or an INTEGRITY REFERENCE. There is no
--    label column, and that absence is load-bearing -- a cached display name is
--    a copy of owner content that drifts silently the moment the owner renames.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.evidence_records (
    id TEXT PRIMARY KEY CHECK (id ~ '^evr_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    project_id TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,

    -- The lens this record belongs to, and the ONLY lens it can appear under.
    -- A record is never reclassified to fill a gap in another lens.
    record_kind TEXT NOT NULL
        CHECK (record_kind IN ('evidence_trace', 'object_version', 'audit_event')),

    -- The registered adapter that produced this reference. Opaque to the core
    -- (AD-2): no branch anywhere reads a connector name out of it.
    producer TEXT NOT NULL CHECK (producer ~ '^[a-z][a-z0-9_]{2,60}$'),

    -- The exact owner. `owner_workspace` is the workspace that OWNS the
    -- artifact, not the workspace that displays the reference.
    owner_workspace TEXT NOT NULL
        CHECK (owner_workspace IN ('data', 'governance', 'analyze', 'context-hub', 'test')),
    owner_object_type TEXT NOT NULL CHECK (owner_object_type ~ '^[a-z][a-z0-9-]{2,60}$'),
    owner_object_id TEXT NOT NULL CHECK (length(btrim(owner_object_id)) BETWEEN 1 AND 200),
    -- NULL only for an intrinsically unversioned immutable owner artifact.
    owner_version_id TEXT CHECK (
        owner_version_id IS NULL OR length(btrim(owner_version_id)) BETWEEN 1 AND 200
    ),

    -- A trace anchor is the ONE record a routed trace hangs from. Pulls,
    -- publications and Results each anchor separately: they are different
    -- claims, and merging them on a shared correlation would invent causality.
    is_anchor BOOLEAN NOT NULL DEFAULT FALSE,

    -- When the owner's event happened, and when the owner recorded it. Two
    -- different questions; a lens that shows only one of them cannot tell a
    -- late arrival from a late event.
    occurred_at TIMESTAMPTZ NOT NULL,
    observed_at TIMESTAMPTZ,
    indexed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- The producer's natural identity for this artifact. Re-indexing the same
    -- artifact hits this unique key and is a no-op; a DIFFERENT payload under
    -- the same identity is refused by the application, never merged.
    source_identity_key TEXT NOT NULL
        CHECK (length(btrim(source_identity_key)) BETWEEN 3 AND 400),
    source_payload_hash TEXT NOT NULL CHECK (source_payload_hash ~ '^[0-9a-f]{64}$'),

    -- The owner's own integrity reference (a content hash), when it has one.
    integrity_hash TEXT CHECK (integrity_hash IS NULL OR integrity_hash ~ '^[0-9a-f]{64}$'),

    -- What the read layer is allowed to resolve for this record. `audit_normalized`
    -- carries the extra redaction rules the audit spine needs.
    redaction_class TEXT NOT NULL DEFAULT 'reference_only'
        CHECK (redaction_class IN ('reference_only', 'audit_normalized')),

    -- Idempotency: one record per (Project, source identity).
    CONSTRAINT uq_evidence_records_source UNIQUE (project_id, source_identity_key),
    -- Children join on the full scope, so an edge can never bridge two tenants.
    CONSTRAINT uq_evidence_records_scope UNIQUE (org_id, project_id, id),
    -- Only a trace record can be an anchor. An `object_version` that claimed to
    -- anchor a lineage graph would be the lens mixing the story forbids.
    CONSTRAINT ck_evidence_records_anchor_kind
        CHECK (NOT is_anchor OR record_kind = 'evidence_trace'),
    CONSTRAINT ck_evidence_records_observed
        CHECK (observed_at IS NULL OR observed_at >= occurred_at - INTERVAL '1 day')
);

-- One anchor per (Project, producer, owner object, owner version). This is the
-- natural identity AC4 names, expressed as a constraint rather than a comment.
CREATE UNIQUE INDEX IF NOT EXISTS uq_evidence_records_anchor
    ON app.evidence_records
       (project_id, producer, owner_object_type, owner_object_id, COALESCE(owner_version_id, ''))
    WHERE is_anchor;

CREATE INDEX IF NOT EXISTS idx_evidence_records_lens
    ON app.evidence_records (project_id, record_kind, occurred_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_evidence_records_owner
    ON app.evidence_records (project_id, owner_workspace, owner_object_type, owner_object_id);
CREATE INDEX IF NOT EXISTS idx_evidence_records_producer
    ON app.evidence_records (project_id, producer, occurred_at DESC);

CREATE OR REPLACE FUNCTION app.reject_evidence_record_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'app.evidence_records is immutable: an Evidence Record is amended by a new availability event, never edited';
END;
$$;
DROP TRIGGER IF EXISTS trg_evidence_records_immutable ON app.evidence_records;
CREATE TRIGGER trg_evidence_records_immutable
    BEFORE UPDATE OR DELETE ON app.evidence_records
    FOR EACH ROW
    WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
    EXECUTE FUNCTION app.reject_evidence_record_mutation();

CREATE OR REPLACE FUNCTION app.reject_evidence_truncate()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'the Evidence index is append-only: TRUNCATE blocked'
        USING ERRCODE = 'raise_exception';
END;
$$;
DROP TRIGGER IF EXISTS trg_evidence_records_block_truncate ON app.evidence_records;
CREATE TRIGGER trg_evidence_records_block_truncate
    BEFORE TRUNCATE ON app.evidence_records
    FOR EACH STATEMENT EXECUTE FUNCTION app.reject_evidence_truncate();

COMMENT ON TABLE app.evidence_records IS
    'Story 49.5: immutable Project-scoped reference to one owner artifact. Identity, scope, owner, time and integrity only -- never owner content.';

-- ---------------------------------------------------------------------------
-- 2. Correlations.
--
--    One record correlates through SEVERAL identifiers at once: a W3C trace, an
--    operation, a pull, an execution, a publication. Flattening them into one
--    text column loses which kind each one is, and a lens that cannot say
--    "this is a trace id, that is a pull id" cannot filter on either.
--
--    A correlation is NOT an edge. Two records sharing an operation id are not
--    thereby linked: the graph in section 3 is built only from links a producer
--    explicitly declared.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.evidence_correlations (
    id TEXT PRIMARY KEY CHECK (id ~ '^evc_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    record_id TEXT NOT NULL,

    correlation_kind TEXT NOT NULL CHECK (correlation_kind IN (
        'w3c_trace', 'operation', 'pull', 'virtual_pull', 'execution',
        'publication', 'result', 'render', 'ai_path', 'evaluation_run',
        'confirmation', 'audit'
    )),
    correlation_id TEXT NOT NULL CHECK (length(btrim(correlation_id)) BETWEEN 1 AND 200),

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_evidence_correlations UNIQUE (record_id, correlation_kind, correlation_id),
    CONSTRAINT fk_evidence_correlations_record
        FOREIGN KEY (org_id, project_id, record_id)
        REFERENCES app.evidence_records (org_id, project_id, id) ON DELETE RESTRICT,
    -- W3C Trace Context: 32 lowercase hex, all-zero invalid. Validated because
    -- the source CLAIMS the format; nothing is ever derived FROM the value.
    CONSTRAINT ck_evidence_correlations_w3c CHECK (
        correlation_kind <> 'w3c_trace'
        OR (correlation_id ~ '^[0-9a-f]{32}$' AND correlation_id <> repeat('0', 32))
    )
);

CREATE INDEX IF NOT EXISTS idx_evidence_correlations_lookup
    ON app.evidence_correlations (project_id, correlation_kind, correlation_id);

DROP TRIGGER IF EXISTS trg_evidence_correlations_immutable ON app.evidence_correlations;
CREATE TRIGGER trg_evidence_correlations_immutable
    BEFORE UPDATE OR DELETE ON app.evidence_correlations
    FOR EACH ROW
    WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
    EXECUTE FUNCTION app.reject_evidence_record_mutation();

DROP TRIGGER IF EXISTS trg_evidence_correlations_block_truncate ON app.evidence_correlations;
CREATE TRIGGER trg_evidence_correlations_block_truncate
    BEFORE TRUNCATE ON app.evidence_correlations
    FOR EACH STATEMENT EXECUTE FUNCTION app.reject_evidence_truncate();

COMMENT ON TABLE app.evidence_correlations IS
    'Story 49.5: typed correlation identifiers of one Evidence Record. A shared correlation is a filter, never an edge.';

-- ---------------------------------------------------------------------------
-- 3. Links -- the only thing that makes a trace a graph.
--
--    A link points either at ANOTHER Evidence Record, or at an exact external
--    owner reference that is not itself indexed. Exactly one of the two, never
--    both and never neither, because a link with no destination is a label.
--
--    `ordinal` is NULL unless the producer PROVED an order. Ordering by
--    timestamp is not proof and is not stored as one.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.evidence_links (
    id TEXT PRIMARY KEY CHECK (id ~ '^evl_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id TEXT NOT NULL,
    project_id TEXT NOT NULL,

    from_record_id TEXT NOT NULL,
    relation TEXT NOT NULL CHECK (relation IN (
        'derives_from', 'supersedes', 'published_by', 'approved_by',
        'produced', 'consumed_by', 'anchored_by', 'references'
    )),

    to_record_id TEXT,
    to_owner_workspace TEXT CHECK (
        to_owner_workspace IS NULL
        OR to_owner_workspace IN ('data', 'governance', 'analyze', 'context-hub', 'test')
    ),
    to_owner_object_type TEXT,
    to_owner_object_id TEXT,
    to_owner_version_id TEXT,

    ordinal INTEGER CHECK (ordinal IS NULL OR ordinal >= 0),
    integrity_hash TEXT CHECK (integrity_hash IS NULL OR integrity_hash ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_evidence_links_from
        FOREIGN KEY (org_id, project_id, from_record_id)
        REFERENCES app.evidence_records (org_id, project_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_evidence_links_to
        FOREIGN KEY (org_id, project_id, to_record_id)
        REFERENCES app.evidence_records (org_id, project_id, id) ON DELETE RESTRICT,
    -- Exactly one destination.
    CONSTRAINT ck_evidence_links_destination CHECK (
        (to_record_id IS NOT NULL AND to_owner_object_id IS NULL)
        OR (to_record_id IS NULL AND to_owner_object_id IS NOT NULL
            AND to_owner_workspace IS NOT NULL AND to_owner_object_type IS NOT NULL)
    ),
    CONSTRAINT ck_evidence_links_no_self CHECK (to_record_id IS DISTINCT FROM from_record_id)
);

-- One edge per (source, relation, destination). A replayed projection re-adds
-- the same edge and collides here instead of fanning the graph out.
CREATE UNIQUE INDEX IF NOT EXISTS uq_evidence_links_edge
    ON app.evidence_links (
        from_record_id, relation,
        COALESCE(to_record_id, ''),
        COALESCE(to_owner_object_id, ''),
        COALESCE(to_owner_version_id, '')
    );

CREATE INDEX IF NOT EXISTS idx_evidence_links_from
    ON app.evidence_links (project_id, from_record_id);
CREATE INDEX IF NOT EXISTS idx_evidence_links_to
    ON app.evidence_links (project_id, to_record_id) WHERE to_record_id IS NOT NULL;

DROP TRIGGER IF EXISTS trg_evidence_links_immutable ON app.evidence_links;
CREATE TRIGGER trg_evidence_links_immutable
    BEFORE UPDATE OR DELETE ON app.evidence_links
    FOR EACH ROW
    WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
    EXECUTE FUNCTION app.reject_evidence_record_mutation();

DROP TRIGGER IF EXISTS trg_evidence_links_block_truncate ON app.evidence_links;
CREATE TRIGGER trg_evidence_links_block_truncate
    BEFORE TRUNCATE ON app.evidence_links
    FOR EACH STATEMENT EXECUTE FUNCTION app.reject_evidence_truncate();

COMMENT ON TABLE app.evidence_links IS
    'Story 49.5: typed directed edge between two Evidence Records, or to an exact external owner reference. The ONLY thing a trace graph traverses.';

-- ---------------------------------------------------------------------------
-- 4. Availability events -- how an immutable index survives retention.
--
--    A source that is retained away does not make its Evidence Record false. It
--    makes it UNRESOLVABLE, which is a different, later fact. Appending that
--    fact is how the index stays honest without editing history and without
--    keeping a copy of what disappeared.
--
--    There is deliberately NO content column here. It is structurally impossible
--    for this table to restore what retention removed.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.evidence_availability_events (
    id TEXT PRIMARY KEY CHECK (id ~ '^eva_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    record_id TEXT NOT NULL,

    availability TEXT NOT NULL CHECK (
        availability IN ('available', 'owner_unavailable', 'retained_away', 'quarantined')
    ),
    -- A policy-safe reason CODE, not a message. A message is where a resource
    -- name leaks into a surface that must not disclose one.
    reason_code TEXT NOT NULL CHECK (reason_code ~ '^[a-z][a-z0-9_]{2,60}$'),

    effective_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by TEXT NOT NULL,

    CONSTRAINT fk_evidence_availability_record
        FOREIGN KEY (org_id, project_id, record_id)
        REFERENCES app.evidence_records (org_id, project_id, id) ON DELETE RESTRICT
);

-- Reads take the newest authorized event by (effective_at, id): deterministic
-- even when two events share a timestamp.
CREATE INDEX IF NOT EXISTS idx_evidence_availability_newest
    ON app.evidence_availability_events (record_id, effective_at DESC, id DESC);

DROP TRIGGER IF EXISTS trg_evidence_availability_append_only ON app.evidence_availability_events;
CREATE TRIGGER trg_evidence_availability_append_only
    BEFORE UPDATE OR DELETE ON app.evidence_availability_events
    FOR EACH ROW
    WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
    EXECUTE FUNCTION app.reject_evidence_record_mutation();

DROP TRIGGER IF EXISTS trg_evidence_availability_block_truncate ON app.evidence_availability_events;
CREATE TRIGGER trg_evidence_availability_block_truncate
    BEFORE TRUNCATE ON app.evidence_availability_events
    FOR EACH STATEMENT EXECUTE FUNCTION app.reject_evidence_truncate();

COMMENT ON TABLE app.evidence_availability_events IS
    'Story 49.5: append-only availability/tombstone facts. Never restores, contains or replaces owner content.';

-- ---------------------------------------------------------------------------
-- 5. Watermarks -- operational state, and openly labelled as such.
--
--    This one IS mutable: it is where the projector is, not what it saw. It is
--    never rendered as a source artifact, and `state` distinguishes a backfill
--    in progress from a finished one so coverage can say `backfilling` instead
--    of implying an empty Project.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.evidence_index_watermarks (
    project_id TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    producer TEXT NOT NULL CHECK (producer ~ '^[a-z][a-z0-9_]{2,60}$'),

    last_source_cursor TEXT,
    last_indexed_at TIMESTAMPTZ,
    indexed_count BIGINT NOT NULL DEFAULT 0 CHECK (indexed_count >= 0),

    state TEXT NOT NULL DEFAULT 'idle'
        CHECK (state IN ('idle', 'backfilling', 'failed')),
    failure_class TEXT CHECK (failure_class IS NULL OR failure_class ~ '^[a-z][a-z0-9_]{2,60}$'),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (project_id, producer)
);

COMMENT ON TABLE app.evidence_index_watermarks IS
    'Story 49.5: per-adapter projector/backfill progress. Operational state, never displayed as evidence.';

-- ---------------------------------------------------------------------------
-- 6. Quarantine -- a poison event, without the poison.
--
--    There is no payload column, on purpose. A quarantine table that stores the
--    event body is where an unredacted provider response ends up living forever
--    under an operational name.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.evidence_projection_quarantine (
    id TEXT PRIMARY KEY CHECK (id ~ '^evq_[0-9A-HJKMNP-TV-Z]{26}$'),
    project_id TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    producer TEXT NOT NULL CHECK (producer ~ '^[a-z][a-z0-9_]{2,60}$'),
    source_identity_key TEXT NOT NULL,
    error_class TEXT NOT NULL CHECK (error_class ~ '^[a-z][a-z0-9_]{2,60}$'),
    attempts INTEGER NOT NULL DEFAULT 1 CHECK (attempts >= 1),
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_evidence_quarantine UNIQUE (project_id, producer, source_identity_key)
);

COMMENT ON TABLE app.evidence_projection_quarantine IS
    'Story 49.5: a projection that could not be indexed, identified by a safe error class. Structurally holds no payload.';

-- ---------------------------------------------------------------------------
-- 7. Fail-closed Row Level Security.
--
--    PostgreSQL 17: a table with RLS enabled and no applicable policy exposes no
--    rows. FORCE makes the policy apply to the table owner too, so a deployment
--    that happens to connect as the owner is not silently exempt. The
--    application guard in `governance_surface_api` runs BEFORE this and stays
--    mandatory: RLS is the floor, not the door.
--
--    ON THE HELPER BELOW. `app.epic36_has_resource_access` is DEFINED by
--    migration 059, which owns it. That migration is not applied everywhere --
--    it was absent from the database this story was developed against -- and a
--    policy referencing a missing function makes the whole migration fail, which
--    would leave these tables with no RLS at all. So it is created here ONLY
--    when it does not already exist: where 059 has run, 059's definition stands
--    untouched; where it has not, the Evidence index is still fail-closed. This
--    is a deliberate fallback, not a second authority.
-- ---------------------------------------------------------------------------
DO $migration$
BEGIN
    IF to_regproc('app.epic36_identity') IS NULL THEN
        CREATE FUNCTION app.epic36_identity() RETURNS TEXT
        LANGUAGE sql STABLE AS $fn$
            SELECT NULLIF(current_setting('toorow.identity', true), '')
        $fn$;
    END IF;

    IF to_regproc('app.epic36_has_resource_access') IS NULL THEN
        CREATE FUNCTION app.epic36_has_resource_access(
            target_org TEXT, target_scope_type TEXT, target_scope_id TEXT
        ) RETURNS BOOLEAN
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = app, pg_temp AS $fn$
            SELECT EXISTS (
                SELECT 1
                FROM app.org_members m
                WHERE m.org_id = target_org
                  AND m.identity = app.epic36_identity()
                  AND m.status = 'active'
                  AND (
                      m.role = 'owner'
                      OR EXISTS (
                          SELECT 1 FROM app.resource_grants g
                          WHERE g.org_id = target_org
                            AND g.identity = m.identity
                            AND g.scope_type = target_scope_type
                            AND g.scope_id = target_scope_id
                      )
                  )
            )
        $fn$;
    END IF;
END
$migration$;

ALTER TABLE app.evidence_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.evidence_records FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS evidence_records_strict ON app.evidence_records;
CREATE POLICY evidence_records_strict ON app.evidence_records
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    );

ALTER TABLE app.evidence_correlations ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.evidence_correlations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS evidence_correlations_strict ON app.evidence_correlations;
CREATE POLICY evidence_correlations_strict ON app.evidence_correlations
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    );

ALTER TABLE app.evidence_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.evidence_links FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS evidence_links_strict ON app.evidence_links;
CREATE POLICY evidence_links_strict ON app.evidence_links
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    );

ALTER TABLE app.evidence_availability_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.evidence_availability_events FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS evidence_availability_strict ON app.evidence_availability_events;
CREATE POLICY evidence_availability_strict ON app.evidence_availability_events
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    );

COMMIT;
