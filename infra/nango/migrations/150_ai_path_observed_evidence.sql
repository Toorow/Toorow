-- Story 49.6: the Context Hub AI Path owner -- immutable OBSERVED evidence.
--
-- WHY THIS EXISTS. Story 49.5 registered `context_ai_path` as a declared but
-- undelivered Evidence producer and said so out loud rather than pointing the
-- address at something else:
--
--     reason_code="ai_path_owner_not_delivered"
--     "`app.context_path_resolutions` is a path-resolution trace, not a
--      canonical AI Path object. Relabelling it as one would put a resolution
--      record under an address that must resolve to an authored Path."
--
-- These two tables are that missing owner. Story 50.1 AC7 cannot be satisfied
-- without them: a Result must point at accepted immutable AI Path evidence, or
-- carry the exact literal `No AI path`. Absence, `null` and `deferred` are all
-- refused there, so the owner had to exist before the Result did.
--
-- WHAT AN AI PATH IS. A record of what was OBSERVED during one AI execution:
-- which calls were made, in which order, against which exact governed object
-- versions, and how each one ended. Nothing else.
--
-- WHAT IT IS NOT, and these are the failure modes it is shaped to prevent:
--
--   * NOT chain-of-thought. Only observable calls, identifiers, versions, order
--     and outcomes are storable. There is no column for model reasoning, and
--     that absence is load-bearing -- a nullable `rationale` column is an
--     invitation to fill it with an inference.
--   * NOT an expected route. `app.context_path_resolutions` answers "which path
--     SHOULD this question take"; an AI Path answers "which path DID this
--     execution take". Turning the first into the second is the exact
--     relabelling 49.5 refused, so neither table references the other as an
--     identity.
--   * NOT an Evaluation Run. Test owns verdicts. What lives here is the
--     observation a verdict is later formed against.
--   * NOT authored content. Knowledge and Skills are authored and versioned by
--     their own owners; a path PINS their versions and never copies them.
--
-- `No AI path` IS NOT A ROW. Human-only work produces no AI Path at all. The
-- exact literal that Story 50.1 stores is a read-model contract in
-- `server/core/ai_paths.py` (`NO_AI_PATH`), not a sentinel row here. A sentinel
-- row would be a fabricated path, and would sort, count and index like a real
-- one.
--
-- IMMUTABILITY IS THE POINT. Evidence that can be edited afterwards is not
-- evidence. Steps are append-only from birth. A path header is mutable ONLY
-- along the single transition `recording -> finalized`; once finalized it
-- refuses UPDATE entirely. DELETE is refused on both, except under the RGPD
-- erasure hatch (`app.rgpd_erasure`, migrations 098/099) so an organization can
-- still be erased -- `org_purge.py` walks the FK graph, so these tables join it
-- automatically and no allowlist needs editing.
--
-- ASSESSMENT IS DERIVED, NEVER STORED AS A VERDICT. The policy snapshot is
-- pinned on the header BEFORE the execution runs. Required/missing/forbidden/
-- alternative/out-of-order/version-mismatch are computed at read time from that
-- pinned snapshot against the observed steps. Storing the verdict instead would
-- make a historical path re-judge itself under today's policy, which is the
-- same defect as reconstructing evidence from "current" state.
--
-- Schema-Change-Checklist (CONTRIBUTING.md):
--   [x] Additive & idempotent (IF NOT EXISTS everywhere; the file is replayable)
--   [x] New columns are NULL-able or have defaults
--   [x] No destructive DROP/ALTER on populated columns
--
-- Numbering: the ledger read 149/149 applied when this was written and the
-- manifest held 149 entries. 150 is the next free identifier.
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- ---------------------------------------------------------------------------
-- 1. The AI Path header.
--
--    One row = "one AI execution was observed, in this exact Project, against
--    this exact pinned policy". Stable opaque id, Project scope, the times, the
--    correlation identifiers, and the pinned pre-query policy snapshot.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.ai_paths (
    id TEXT PRIMARY KEY CHECK (id ~ '^aip_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    project_id TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,

    -- `recording` accepts appended steps. `finalized` accepts nothing further.
    -- There is no `cancelled`: an execution that stopped is a FAILED or
    -- UNAVAILABLE observation, and saying so is the honest record.
    lifecycle TEXT NOT NULL DEFAULT 'recording'
        CHECK (lifecycle IN ('recording', 'finalized')),

    -- What was OBSERVED to happen, set at finalization.
    --   succeeded   -- the execution completed and its steps were captured
    --   failed      -- it ended in error, and the observed steps are kept
    --   refused     -- it was declined by policy before producing an answer
    --   unavailable -- instrumentation was absent or lost; nothing is inferred
    outcome TEXT CHECK (
        outcome IS NULL
        OR outcome IN ('succeeded', 'failed', 'refused', 'unavailable')
    ),

    -- An opaque W3C correlation id when instrumentation supplies one. 32 lower
    -- hex, and never all-zero: the all-zero trace id is the W3C "invalid" value
    -- and a stored one would correlate every uninstrumented execution together.
    -- It does NOT replace `id`: correlation is not identity.
    w3c_trace_id TEXT CHECK (
        w3c_trace_id IS NULL
        OR (w3c_trace_id ~ '^[0-9a-f]{32}$' AND w3c_trace_id <> repeat('0', 32))
    ),

    -- Who ran it and which execution it belongs to. `actor` is an identity, not
    -- a display name.
    actor TEXT NOT NULL CHECK (length(btrim(actor)) BETWEEN 1 AND 200),
    execution_correlation TEXT CHECK (
        execution_correlation IS NULL
        OR length(btrim(execution_correlation)) BETWEEN 1 AND 200
    ),

    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ,

    -- The exact governed references that were actually observed for the run as
    -- a whole. Each is a reference, never a copy of the referenced content.
    model_ref TEXT CHECK (model_ref IS NULL OR length(btrim(model_ref)) BETWEEN 1 AND 200),
    tool_catalog_version TEXT CHECK (
        tool_catalog_version IS NULL OR length(btrim(tool_catalog_version)) BETWEEN 1 AND 200
    ),

    -- The pre-query policy, pinned BEFORE execution. Read-time assessment is
    -- computed against THIS, never against today's policy.
    policy_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(policy_snapshot) = 'object'),
    policy_snapshot_hash TEXT NOT NULL CHECK (policy_snapshot_hash ~ '^[0-9a-f]{64}$'),

    -- Integrity of the finalized observation. Absent while recording.
    content_hash TEXT CHECK (content_hash IS NULL OR content_hash ~ '^[0-9a-f]{64}$'),

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Children join on the full scope, so a step can never bridge two tenants.
    CONSTRAINT uq_ai_paths_scope UNIQUE (id, org_id, project_id),

    -- A finalized path is a complete claim or it is not finalized. Half of one
    -- is what makes an evidence chain unreadable six months later.
    CONSTRAINT ck_ai_paths_finalized_is_complete CHECK (
        lifecycle <> 'finalized'
        OR (outcome IS NOT NULL AND ended_at IS NOT NULL AND content_hash IS NOT NULL)
    ),
    CONSTRAINT ck_ai_paths_recording_is_open CHECK (
        lifecycle <> 'recording'
        OR (outcome IS NULL AND ended_at IS NULL AND content_hash IS NULL)
    ),
    CONSTRAINT ck_ai_paths_ends_after_start CHECK (
        ended_at IS NULL OR ended_at >= started_at
    )
);

CREATE INDEX IF NOT EXISTS idx_ai_paths_project_started
    ON app.ai_paths (project_id, started_at DESC, id);

CREATE INDEX IF NOT EXISTS idx_ai_paths_trace
    ON app.ai_paths (project_id, w3c_trace_id)
    WHERE w3c_trace_id IS NOT NULL;

COMMENT ON TABLE app.ai_paths IS
    'Story 49.6: one observed AI execution. Immutable once finalized. Never chain-of-thought, never an expected route.';

-- ---------------------------------------------------------------------------
-- 2. Ordered, append-only observed steps.
--
--    A step records one observable call: what was reached, which exact version,
--    and how it ended. `skill_version_id` + `skill_step_id` are pinned together
--    because a step id alone is meaningless across Skill versions -- that pair
--    is the contract Story 49.6 AC3 names.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.ai_path_steps (
    id TEXT PRIMARY KEY CHECK (id ~ '^aps_[0-9A-HJKMNP-TV-Z]{26}$'),
    path_id TEXT NOT NULL,
    org_id TEXT NOT NULL,
    project_id TEXT NOT NULL,

    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    step_kind TEXT NOT NULL CHECK (step_kind IN (
        'tool_call', 'knowledge_read', 'skill_step', 'semantic_query',
        'data_read', 'handoff'
    )),

    -- The exact governed object this step reached, when it reached one. A step
    -- that reached nothing governed (a plain tool call) leaves these NULL
    -- rather than being dropped: an unrepresented step stays visible in the
    -- path instead of inventing a graph object for it.
    owner_workspace TEXT CHECK (
        owner_workspace IS NULL
        OR owner_workspace IN ('data', 'governance', 'analyze', 'context-hub', 'test')
    ),
    owner_object_type TEXT CHECK (
        owner_object_type IS NULL OR owner_object_type ~ '^[a-z][a-z0-9-]{2,60}$'
    ),
    owner_object_id TEXT CHECK (
        owner_object_id IS NULL OR length(btrim(owner_object_id)) BETWEEN 1 AND 200
    ),
    owner_version_id TEXT CHECK (
        owner_version_id IS NULL OR length(btrim(owner_version_id)) BETWEEN 1 AND 200
    ),

    -- The Skill step pin. Both or neither: half a pin resolves to the wrong
    -- step the first time a Skill is revised.
    skill_version_id TEXT CHECK (
        skill_version_id IS NULL OR length(btrim(skill_version_id)) BETWEEN 1 AND 200
    ),
    skill_step_id TEXT CHECK (
        skill_step_id IS NULL OR length(btrim(skill_step_id)) BETWEEN 1 AND 200
    ),

    -- A free label for the observable call, for steps with no governed owner.
    -- It is a LABEL, not an identity: nothing joins on it.
    tool_name TEXT CHECK (tool_name IS NULL OR length(btrim(tool_name)) BETWEEN 1 AND 200),

    outcome TEXT NOT NULL CHECK (
        outcome IN ('succeeded', 'failed', 'refused', 'unavailable')
    ),

    -- An exact reference into the Story 49.5 Evidence index, when one exists.
    evidence_record_id TEXT CHECK (
        evidence_record_id IS NULL OR evidence_record_id ~ '^evr_[0-9A-HJKMNP-TV-Z]{26}$'
    ),

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- The scope-carrying parent link. A composite FK, not a comment: a step
    -- cannot name a path belonging to another Project.
    CONSTRAINT fk_ai_path_steps_scope
        FOREIGN KEY (path_id, org_id, project_id)
        REFERENCES app.ai_paths (id, org_id, project_id) ON DELETE RESTRICT,

    -- Order is a fact of the observation, so it is unique and gapless-checked
    -- by the service rather than re-derived from timestamps that can tie.
    CONSTRAINT uq_ai_path_steps_ordinal UNIQUE (path_id, ordinal),

    CONSTRAINT ck_ai_path_steps_skill_pin CHECK (
        (skill_version_id IS NULL) = (skill_step_id IS NULL)
    ),
    -- An owner reference is a workspace + type + id, or it is not a reference.
    CONSTRAINT ck_ai_path_steps_owner_complete CHECK (
        (owner_workspace IS NULL AND owner_object_type IS NULL AND owner_object_id IS NULL)
        OR (owner_workspace IS NOT NULL AND owner_object_type IS NOT NULL
            AND owner_object_id IS NOT NULL)
    ),
    -- A version pin with nothing to pin it to is a dangling claim.
    CONSTRAINT ck_ai_path_steps_version_needs_owner CHECK (
        owner_version_id IS NULL OR owner_object_id IS NOT NULL
    )
);

CREATE INDEX IF NOT EXISTS idx_ai_path_steps_path
    ON app.ai_path_steps (path_id, ordinal);

CREATE INDEX IF NOT EXISTS idx_ai_path_steps_owner
    ON app.ai_path_steps (project_id, owner_workspace, owner_object_type, owner_object_id)
    WHERE owner_object_id IS NOT NULL;

COMMENT ON TABLE app.ai_path_steps IS
    'Story 49.6: ordered append-only observable calls of one AI Path. Never edited, never reordered.';

-- ---------------------------------------------------------------------------
-- 3. Immutability.
--
--    The RGPD guard on every trigger is what keeps organization erasure
--    working: `org_purge.py` sets `app.rgpd_erasure` and walks the FK graph, so
--    these tables need no allowlist entry -- but without the guard they would
--    silently block the erasure of any org that ever ran an AI execution.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION app.reject_ai_path_step_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'AI Path steps are append-only observed evidence'
        USING ERRCODE = '23000';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_ai_path_steps_append_only ON app.ai_path_steps;
CREATE TRIGGER trg_ai_path_steps_append_only
BEFORE UPDATE OR DELETE ON app.ai_path_steps
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_ai_path_step_mutation();

-- A step may only be appended while its path is still recording. Without this,
-- a finalized path could grow a new step and its `content_hash` would silently
-- stop describing it.
CREATE OR REPLACE FUNCTION app.reject_step_on_finalized_path()
RETURNS TRIGGER AS $$
DECLARE
    path_lifecycle TEXT;
BEGIN
    SELECT lifecycle INTO path_lifecycle FROM app.ai_paths WHERE id = NEW.path_id;
    IF path_lifecycle IS DISTINCT FROM 'recording' THEN
        RAISE EXCEPTION 'cannot append a step to a finalized AI Path'
            USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_ai_path_steps_only_while_recording ON app.ai_path_steps;
CREATE TRIGGER trg_ai_path_steps_only_while_recording
BEFORE INSERT ON app.ai_path_steps
FOR EACH ROW EXECUTE FUNCTION app.reject_step_on_finalized_path();

-- The header allows exactly one transition, and freezes afterwards. Everything
-- else -- re-opening, re-outcoming, rewriting the pinned policy, moving the
-- start time -- is refused at the row level rather than by convention.
CREATE OR REPLACE FUNCTION app.reject_ai_path_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'AI Paths are immutable observed evidence'
            USING ERRCODE = '23000';
    END IF;

    IF OLD.lifecycle = 'finalized' THEN
        RAISE EXCEPTION 'a finalized AI Path cannot be modified'
            USING ERRCODE = '23000';
    END IF;

    IF NEW.lifecycle <> 'finalized' THEN
        RAISE EXCEPTION 'the only legal AI Path transition is recording -> finalized'
            USING ERRCODE = '23000';
    END IF;

    IF NEW.id <> OLD.id
       OR NEW.org_id <> OLD.org_id
       OR NEW.project_id <> OLD.project_id
       OR NEW.actor <> OLD.actor
       OR NEW.started_at <> OLD.started_at
       OR NEW.policy_snapshot_hash <> OLD.policy_snapshot_hash
       OR NEW.policy_snapshot IS DISTINCT FROM OLD.policy_snapshot
       OR NEW.w3c_trace_id IS DISTINCT FROM OLD.w3c_trace_id
    THEN
        RAISE EXCEPTION 'finalization may not rewrite the observed request facts'
            USING ERRCODE = '23000';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_ai_paths_immutable ON app.ai_paths;
CREATE TRIGGER trg_ai_paths_immutable
BEFORE UPDATE OR DELETE ON app.ai_paths
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_ai_path_mutation();

CREATE OR REPLACE FUNCTION app.reject_ai_path_truncate()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'AI Path evidence cannot be truncated'
        USING ERRCODE = '23000';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_ai_paths_block_truncate ON app.ai_paths;
CREATE TRIGGER trg_ai_paths_block_truncate
BEFORE TRUNCATE ON app.ai_paths
FOR EACH STATEMENT EXECUTE FUNCTION app.reject_ai_path_truncate();

DROP TRIGGER IF EXISTS trg_ai_path_steps_block_truncate ON app.ai_path_steps;
CREATE TRIGGER trg_ai_path_steps_block_truncate
BEFORE TRUNCATE ON app.ai_path_steps
FOR EACH STATEMENT EXECUTE FUNCTION app.reject_ai_path_truncate();

-- ---------------------------------------------------------------------------
-- 4. Fail-closed Row Level Security.
--
--    Same contract as migration 149: RLS enabled with no applicable policy
--    exposes no rows, and FORCE applies the policy to the table owner too, so a
--    deployment connecting as owner is not silently exempt. The application
--    authorization check runs BEFORE this and stays mandatory -- RLS is the
--    floor, not the door.
--
--    `app.epic36_has_resource_access` is owned by migration 059 and created
--    defensively by 149 where 059 has not run. It is only referenced here.
-- ---------------------------------------------------------------------------
ALTER TABLE app.ai_paths ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.ai_paths FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS ai_paths_strict ON app.ai_paths;
CREATE POLICY ai_paths_strict ON app.ai_paths
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    );

ALTER TABLE app.ai_path_steps ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.ai_path_steps FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS ai_path_steps_strict ON app.ai_path_steps;
CREATE POLICY ai_path_steps_strict ON app.ai_path_steps
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    );

COMMIT;
