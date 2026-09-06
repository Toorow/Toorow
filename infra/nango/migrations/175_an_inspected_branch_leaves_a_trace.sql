-- Story 55.2: looking at the branches not taken is itself an observable act.
--
-- WHY THIS TABLE EXISTS, AND WHY IT IS NOT A LOG.
-- Migration 168 recorded a measured absence: `mcp_app_behavior` (epic 51) judges
-- an INTERACTION PER EXECUTION -- "Render/Result parity, evidence drill-down,
-- honest empty/stale/error/refusal states, read-only View Tools, governed
-- feedback and capability-safe host fallback" (analyze-and-test.md:326) -- and
--
--     SELECT table_name FROM information_schema.tables
--      WHERE table_schema = 'app'
--        AND (table_name LIKE '%interaction%' OR table_name LIKE '%view_tool%'
--             OR table_name LIKE '%drill%');
--     -- zero rows
--
-- Re-measured on 2026-08-01 against a disposable PostgreSQL carrying every
-- applied migration: still zero rows. So the absence 168 recorded was still true
-- the moment this file was written, and this migration is what makes it false.
--
-- Story 55.2 ships the surface that dimension judges: a subtree that expands one
-- step of an AI Path and lists the branches the walk considered and did not take.
-- Shipping that surface WITHOUT recording its use would have delivered the exact
-- object `mcp_app_behavior` grades and left it ungradable -- the "green while
-- false" class this repo keeps repairing.
--
-- WHAT ONE ROW IS. One person, on one surface, opened one step of one walk, at one
-- time, and was shown one honest state. Four facts and a state, nothing else.
-- There is no column for what was read, no column for a dwell time, no column for
-- free text: an inspection record is evidence that a surface was USED, never a
-- second copy of what it displayed. `app.ai_path_steps` already holds the walk;
-- copying its content here would create a second store with a second truth.
--
-- WHY `displayed_state` IS NOT NULLABLE, and why it has four values.
-- The dimension judges HONEST STATES, so the state that was shown is the fact
-- worth recording. And the three ways a branch listing can be empty are three
-- different facts that a single boolean would collapse:
--
--   * `branches_listed`        -- candidates were judged and are shown with their
--                                 fates. `branches_listed` counts them.
--   * `no_branch_judged`       -- the walk ran and judged nothing. An honest zero.
--   * `branches_not_recorded`  -- the walk's candidates were emitted live (Story
--                                 54.2) and no store holds them, so this reader
--                                 cannot know what was judged. NOT a zero.
--   * `unavailable`            -- the branch data could not be read at all.
--
-- Collapsing `branches_not_recorded` into `no_branch_judged` would make "we did
-- not keep this" read as "nothing was considered", which is precisely the
-- never-reached/rejected confusion Story 55.2 AC3 exists to prevent -- restated
-- one level up, about the listing itself.
--
-- APPEND-ONLY, WITH THE ERASURE HATCH ITS SIBLINGS CARRY.
-- An observation that can be edited afterwards is not an observation. UPDATE and
-- DELETE both raise, EXCEPT inside a transaction that sets
-- `SET LOCAL app.rgpd_erasure = 'on'` -- the audited tenant-erasure path of
-- migration 098 that `core/org_purge.py` walks. Migration 169 exists because
-- `app.file_source_templates` was born without that hatch and became unerasable;
-- this table is born with it. Both foreign keys are real, so the purge tree
-- reaches these rows without an allowlist entry.
--
-- NO DEMO CONTENT. This migration inserts no row, and no code fabricates one.
-- The dimension becomes reachable because a person can now be observed using the
-- surface, not because a row was written to make a screen look populated.
--
-- NUMBERING, AND A DELIBERATE DEVIATION STATED RATHER THAN APPLIED IN SILENCE.
-- The session brief for this story assigned the slice "180 and beyond" to keep
-- clear of 172/173, held by the epic-52 session. 180 is not usable:
-- `scripts/check_migration_catalog.py` fails on `missing migration identifiers`
-- for any gap between 001 and the highest file, so a 180 with no 175..179 breaks
-- the catalog and the story's own Definition of Done. Measured before choosing:
-- `python scripts/check_migration_catalog.py` -> `migration catalog OK: 174
-- migrations (001..174)`. 175 is therefore the next free identifier, and it is
-- the one taken.
--
-- Schema-Change-Checklist (CONTRIBUTING.md):
--   [x] Additive & idempotent (IF NOT EXISTS / OR REPLACE everywhere; replayable)
--   [x] New columns are NULL-able or have defaults
--   [x] No destructive DROP/ALTER on populated columns
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- ---------------------------------------------------------------------------
-- 1. The observed inspection.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.evidence_inspections (
    id TEXT PRIMARY KEY CHECK (id ~ '^evi_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    project_id TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,

    -- WHICH RESULT. A reference, never a copy. Left NULL when the surface is the
    -- Context Hub AI Path screen, which reads a walk that belongs to no Result:
    -- inventing a Result id to fill the column would be a fabricated join key.
    result_ref TEXT CHECK (
        result_ref IS NULL OR length(btrim(result_ref)) BETWEEN 1 AND 200
    ),
    -- The rendered artifact the person was looking at, when there is one. This is
    -- the same object `app.evaluation_case_dimension_verdicts.render_ref` pins,
    -- which is what lets a later evaluator line the two up.
    render_ref TEXT CHECK (
        render_ref IS NULL OR length(btrim(render_ref)) BETWEEN 1 AND 200
    ),

    -- WHICH STEP. The walk, and the position inside it that was opened. The
    -- ordinal is the step's own order in the walk, the number the reader saw.
    ai_path_id TEXT CHECK (
        ai_path_id IS NULL OR ai_path_id ~ '^aip_[0-9A-HJKMNP-TV-Z]{26}$'
    ),
    step_ordinal INTEGER CHECK (step_ordinal IS NULL OR step_ordinal >= 0),

    -- WHAT WAS DONE. Closed vocabulary: a free-text kind would turn this table
    -- into a log, and a log is not evidence a dimension can be graded against.
    kind TEXT NOT NULL CHECK (kind IN (
        'branch_subtree_expanded',
        'branch_subtree_collapsed',
        'evidence_drilldown_opened',
        'text_fallback_shown'
    )),

    -- WHERE. The three surfaces that mount the shared visualization runtime.
    -- `mcp_app` is the one epic 51's dimension is named after; the other two are
    -- here because the same drawing serves them and a per-surface table would be
    -- three tables with three truths.
    surface TEXT NOT NULL CHECK (surface IN ('console', 'mcp_app', 'share')),

    -- WHAT WAS SHOWN. See the header: four values, and the fourth is the one a
    -- boolean would have destroyed.
    displayed_state TEXT NOT NULL CHECK (displayed_state IN (
        'branches_listed',
        'no_branch_judged',
        'branches_not_recorded',
        'unavailable'
    )),

    -- How many branches were actually put in front of the person. NULL means
    -- "not applicable to this state", never zero.
    branches_listed INTEGER CHECK (branches_listed IS NULL OR branches_listed >= 0),

    -- WHO and WHEN. `actor` is an identity, exactly as on `app.ai_paths`.
    actor TEXT NOT NULL CHECK (length(btrim(actor)) BETWEEN 1 AND 200),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- The scope-carrying parent link. Composite, so an inspection can never name
    -- a walk belonging to another Project. MATCH SIMPLE: with `ai_path_id` NULL
    -- the constraint does not apply, which is the intended shape -- a surface
    -- that inspected something other than a persisted walk still records the act.
    CONSTRAINT fk_evidence_inspections_path
        FOREIGN KEY (ai_path_id, org_id, project_id)
        REFERENCES app.ai_paths (id, org_id, project_id) ON DELETE RESTRICT,

    -- A step ordinal with no walk to index into is a dangling claim.
    CONSTRAINT ck_evidence_inspections_step_needs_path CHECK (
        step_ordinal IS NULL OR ai_path_id IS NOT NULL
    ),
    -- A listing states how many it listed, or it is not a listing.
    CONSTRAINT ck_evidence_inspections_listing_is_counted CHECK (
        displayed_state <> 'branches_listed' OR branches_listed IS NOT NULL
    ),
    -- ... and the three non-listing states may not carry a count. A `0` beside
    -- `branches_not_recorded` would assert that nothing was judged, which is the
    -- one thing that state exists to refuse to assert.
    CONSTRAINT ck_evidence_inspections_absence_counts_nothing CHECK (
        displayed_state = 'branches_listed' OR branches_listed IS NULL
    )
);

CREATE INDEX IF NOT EXISTS idx_evidence_inspections_project_time
    ON app.evidence_inspections (project_id, occurred_at DESC, id);

-- The lookup an evaluator makes: "was this rendered artifact ever inspected?"
CREATE INDEX IF NOT EXISTS idx_evidence_inspections_render
    ON app.evidence_inspections (project_id, render_ref)
    WHERE render_ref IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_evidence_inspections_path
    ON app.evidence_inspections (ai_path_id, step_ordinal)
    WHERE ai_path_id IS NOT NULL;

COMMENT ON TABLE app.evidence_inspections IS
    'Story 55.2: one observed evidence inspection -- which Result, which step of '
    'which AI Path, which actor, when, and which honest state was shown. '
    'Append-only; DELETE only under the audited app.rgpd_erasure hatch. It is '
    'evidence that a surface was USED, never a second copy of what it displayed.';

COMMENT ON COLUMN app.evidence_inspections.displayed_state IS
    'The state the person was actually shown. `branches_not_recorded` is NOT a '
    'zero: Story 54.2 emits the judged candidates on the live progress stream and '
    'no store holds them, so a reader of a persisted walk cannot know what was '
    'judged. Collapsing it into `no_branch_judged` would make "we did not keep '
    'this" read as "nothing was considered".';

-- ---------------------------------------------------------------------------
-- 2. Append-only, with the erasure hatch.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.reject_evidence_inspection_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'evidence inspections are append-only observed evidence: % of id=% is '
        'forbidden. An audited tenant erasure must run inside a transaction that '
        'sets SET LOCAL app.rgpd_erasure = ''on'' (see migration 098).',
        TG_OP, OLD.id
        USING ERRCODE = '23000';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_evidence_inspections_append_only
    ON app.evidence_inspections;
CREATE TRIGGER trg_evidence_inspections_append_only
BEFORE UPDATE OR DELETE ON app.evidence_inspections
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_evidence_inspection_mutation();

CREATE OR REPLACE FUNCTION app.reject_evidence_inspection_truncate()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'evidence inspections cannot be truncated'
        USING ERRCODE = '23000';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_evidence_inspections_block_truncate
    ON app.evidence_inspections;
CREATE TRIGGER trg_evidence_inspections_block_truncate
BEFORE TRUNCATE ON app.evidence_inspections
FOR EACH STATEMENT EXECUTE FUNCTION app.reject_evidence_inspection_truncate();

-- ---------------------------------------------------------------------------
-- 3. Fail-closed Row Level Security -- the same floor migrations 149/150 set.
--
--    RLS enabled with no applicable policy exposes no rows, and FORCE applies the
--    policy to the table owner too, so a deployment connecting as owner is not
--    silently exempt. The application authorization check runs BEFORE this and
--    stays mandatory: RLS is the floor, not the door.
-- ---------------------------------------------------------------------------
ALTER TABLE app.evidence_inspections ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.evidence_inspections FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS evidence_inspections_strict ON app.evidence_inspections;
CREATE POLICY evidence_inspections_strict ON app.evidence_inspections
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    );

-- ---------------------------------------------------------------------------
-- 4. Migration 168's finding is REMOVED, because it is no longer true.
--
--    168 stated, on the constraint itself: "no table records it: no interaction,
--    view-tool or drill-down relation exists in `app`". Section 1 of this file is
--    that relation. Leaving the sentence in place while shipping the surface it
--    describes is the defect 168 itself warned about -- "a comment that lies is a
--    defect with a long fuse" -- so the sentence is replaced rather than left to
--    be checked and believed by the next reader.
--
--    THE GUARD ITSELF STAYS, and for TWO reasons that are different from the one
--    168 gave. Both are stated so nobody has to re-measure them:
--
--      a. NO EVALUATOR READS THIS TABLE. `core/evaluation_runs.py` still writes
--         `unverifiable` for this dimension unconditionally, and nothing anywhere
--         forms a verdict from an inspection. Story 55.2 deliberately does not
--         decide what `mcp_app_behavior` scores -- that is epic 51's, and grading
--         a dimension from the story that produces its inputs is how an evaluator
--         ends up marking its own homework.
--      b. THE `mcp_app` SURFACE HAS NO WRITER YET, and the reason is a vocabulary
--         gap, not an oversight. The Console writes through
--         `POST /api/projects/{project_id}/context/ai-paths/{path_id}/inspections`
--         (`core/ai_paths_api.py`). The MCP App would need an app-visibility MCP
--         tool, and AD-24's effect vocabulary (`core/mcp_profiles.py:EFFECTS`) has
--         exactly three slots, none of which fits a passive observation recorded
--         by a widget: `read` would be a false declaration (this writes),
--         `confirmed_write` demands a host/human ceremony for an act nobody
--         consented to ceremonially, and `prepare` is only declarable under a
--         profile that fails closed as high-risk. The one precedent for a
--         widget-called write, `submit_feedback`, is registered on plain
--         `mcp.tool` inside `core/main.py`. Adding the slot -- or an app-only
--         exemption -- is a decision to take AT `mcp_profiles.py`, not one to
--         acquire from a story about a drawing.
--
--    So `surface = 'mcp_app'` is a value this table accepts and that nothing
--    writes today. It is left in the CHECK rather than removed, because the shape
--    of the record is decided and only its one caller is missing; deleting the
--    value would erase the inventory of what remains.
-- ---------------------------------------------------------------------------

COMMENT ON CONSTRAINT ck_evaluation_case_verdicts_mcp_evaluator_absent
    ON app.evaluation_case_dimension_verdicts IS
    'A pass here would still be a claim about nothing, but NOT for the reason '
    'migration 168 recorded. That reason -- "no table records an interaction" -- '
    'stopped being true with migration 175: app.evidence_inspections records, per '
    'inspection, which Result, which step of which AI Path, which actor, when, and '
    'which honest state was shown, and the Console writes it through '
    'POST /api/projects/{project_id}/context/ai-paths/{path_id}/inspections. TWO '
    'things are missing now, and neither is the table. (1) No EVALUATOR reads it: '
    'core/evaluation_runs.py still writes `unverifiable` for this dimension '
    'unconditionally. (2) The mcp_app surface has no writer: an app-visibility MCP '
    'tool cannot be declared under the three effects of AD-24 without lying about '
    'one of them, so that slot is a decision at core/mcp_profiles.py. Dropped by '
    'the story that delivers the evaluator (epic 51), and by no other -- Story '
    '55.2 produces this dimension''s inputs and refuses to grade them.';

COMMENT ON COLUMN app.evaluation_case_dimension_verdicts.render_ref IS
    'The Render this verdict judged. Fillable since migration 166 (composite '
    'foreign key to app.renders), which is what the `mcp_needs_evidence` CHECK has '
    'always required for a pass. Since migration 175 it also has a counterpart to '
    'be joined against: app.evidence_inspections.render_ref records that this '
    'exact rendered artifact was actually inspected by someone, which is the '
    'per-execution observation `mcp_app_behavior` judges.';

COMMIT;
