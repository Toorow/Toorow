-- Story 50.3: Reports, Notebooks and Renders as three DISTINCT lifecycle objects.
--
-- WHY THIS EXISTS. Today the repository conflates three different things under
-- names that look alike:
--
--   * `app.project_reports` (migration 014) is a per-Project ENABLEMENT toggle on a
--     connector seed -- `module_name`/`report_id`/`enabled`. It carries no query
--     intent, no version and no history. It is seed availability, not a Report.
--   * `app.notebooks` (migration 015) is a MUTABLE row holding one `report_ref`,
--     one `window_rule` and one prompt. Editing it overwrites the definition every
--     previous Run was produced from, so a Run can no longer be explained.
--   * `app.render_snapshots` (migration 051) stores a `get_card|get_report`
--     envelope and a widget URI. It pins no Result, no Visualization Spec, no
--     runtime build, no theme, no formatter and no evidence manifest, so it cannot
--     be replayed -- only re-displayed from a frozen blob.
--
-- None of those three is renamed, dropped or rewritten here. They stay exactly as
-- they are, readable, and Story 50.3 exposes them as explicitly LEGACY evidence.
-- Story 50.3 AC12 is explicit that migration is additive and non-destructive, and
-- that a legacy row lacking pins is never promoted into a canonical object.
--
-- THE IDENTITY CHAIN this schema adds, on top of Story 50.1's:
--
--     Query Spec version -> Result            (migration 151, unchanged)
--     Report / version   -> Query Spec version + presentation intent
--     Report run         -> Report version + Result (+ optional Render)
--     Notebook / version -> ordered version-pinned blocks
--     Notebook Run       -> Notebook version, and per block: Result + Render|No Render
--     Render             -> one exact Result and TEN replay pins
--
-- Every arrow is a Project-scoped COMPOSITE foreign key, never a bare id, so a row
-- cannot point across Projects even when the application layer is wrong (AC13).
--
-- WHAT IS MUTABLE, and it is a short list: the two stable HEADS
-- (`analysis_reports`, `analysis_notebooks`) may advance their current-version
-- pointer and be archived. The Notebook Run head may advance FORWARD ONLY, to a
-- terminal state, and may never be rebound to another Notebook version -- the same
-- attempt/evidence split migration 151 uses, for the same reason. Everything else
-- is insert-once and the trigger says so.
--
-- THE HONEST HOLE, stated in schema rather than in a comment. AC2 requires a Report
-- version to reference "an accepted Visualization Template version or an accepted
-- materialized Visualization Spec version". Story 50.4 owns those objects and has
-- not landed: `app.visualization_spec_versions` does not exist in this database.
-- Rather than invent a nullable "fill it in later" column -- which is how
-- `envelope_ref = 'deferred'` got into `app.notebook_runs` -- the presentation
-- reference follows migration 151's `ai_path_absent_literal` pattern: EITHER a
-- complete pin, OR the exact literal below, enforced by a CHECK. There is no third
-- state, and the CHECK on `analysis_report_versions` spells the literal out so no
-- caller can invent a different wording.
--
--     'No accepted presentation contract'
--
-- The same discipline guards the canonical Render: every one of its ten replay pins
-- is NOT NULL and every one is refused the four placeholder words Story 50.3 names
-- explicitly -- `legacy`, `current`, `deferred`, `latest`. A Render therefore cannot
-- exist at all until Stories 50.4/50.5 supply real identities, which is the point:
-- an empty `app.renders` is an honest statement, and a table of Renders pinned to
-- 'current' would not be.

BEGIN;

-- ---------------------------------------------------------------------------
-- 0. One shared guard for every replay pin.
--
--    Story 50.3's Implementation Gate forbids filling a pin with `legacy`,
--    `current`, `deferred` or `latest`. Written once, applied to each pin, so a
--    column added later cannot quietly skip it.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.is_exact_pin(value TEXT)
RETURNS BOOLEAN AS $$
    SELECT value IS NOT NULL
       AND btrim(value) <> ''
       AND lower(btrim(value)) NOT IN ('legacy', 'current', 'deferred', 'latest', 'unknown', 'none');
$$ LANGUAGE sql IMMUTABLE;

COMMENT ON FUNCTION app.is_exact_pin(TEXT) IS
    'Story 50.3: a replay or version pin must name an exact identity. The four '
    'words Story 50.3 forbids are rejected here rather than in a service, because '
    'a service can be bypassed and this cannot.';

-- ---------------------------------------------------------------------------
-- 1. Report: a stable head, and immutable versions on top of it.
--
--    The head is reusable INTENT. It is not a cached answer and holds no Result:
--    `analyze-and-test.md` line 50 is explicit that "the Report itself is not a
--    cached answer".
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.analysis_reports (
    id                  TEXT PRIMARY KEY,
    org_id              TEXT NOT NULL,
    project_id          TEXT NOT NULL,
    label               TEXT NOT NULL,
    description         TEXT,
    -- Where this Report came from. A connector expert pack SEEDS a Report; it does
    -- not own it (AC4). The seed coordinates are provenance, never identity, which
    -- is why they are plain columns and not a foreign key into connector JSON.
    seed_origin         TEXT NOT NULL DEFAULT 'project',
    seed_module_name    TEXT,
    seed_report_id      TEXT,
    current_version_id  TEXT,
    -- Archive, never delete: AC12 forbids destroying the only evidence of history,
    -- and every Run below points at a version of this head.
    archived_at         TIMESTAMPTZ,
    created_by          TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_analysis_reports_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT fk_analysis_reports_project
        FOREIGN KEY (org_id, project_id) REFERENCES app.projects (org_id, id),
    CONSTRAINT ck_analysis_reports_label_bounded
        CHECK (char_length(label) BETWEEN 1 AND 200),
    CONSTRAINT ck_analysis_reports_seed_origin
        CHECK (seed_origin IN ('project', 'connector_seed', 'explore')),
    -- A connector seed names BOTH coordinates or neither. Half a seed reference is
    -- how a Report ends up claiming a provenance nobody can resolve.
    CONSTRAINT ck_analysis_reports_seed_pair CHECK (
        (seed_origin = 'connector_seed'
            AND seed_module_name IS NOT NULL AND seed_report_id IS NOT NULL)
        OR (seed_origin <> 'connector_seed'
            AND seed_module_name IS NULL AND seed_report_id IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_analysis_reports_project
    ON app.analysis_reports (project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_analysis_reports_live
    ON app.analysis_reports (project_id, updated_at DESC)
    WHERE archived_at IS NULL;

CREATE TABLE IF NOT EXISTS app.analysis_report_versions (
    id                          TEXT PRIMARY KEY,
    report_id                   TEXT NOT NULL,
    org_id                      TEXT NOT NULL,
    project_id                  TEXT NOT NULL,
    version_number              INTEGER NOT NULL,
    label                       TEXT NOT NULL,
    description                 TEXT,
    -- The analytical half: one EXACT Query Spec version, never the head. A Report
    -- bound to a Query Spec head would silently change meaning the day someone
    -- revised the query (AC2, AC11).
    query_spec_id               TEXT NOT NULL,
    query_spec_version_id       TEXT NOT NULL,
    -- The presentation half. Complete, or honestly absent -- see the header.
    presentation_kind           TEXT,
    presentation_version_id     TEXT,
    presentation_absent_literal TEXT,
    seed_provenance             JSONB NOT NULL DEFAULT '{}'::jsonb,
    content_hash                TEXT NOT NULL,
    predecessor_version_id      TEXT,
    created_by                  TEXT NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_analysis_report_versions_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT uq_analysis_report_version_number UNIQUE (report_id, version_number),
    CONSTRAINT fk_analysis_report_versions_head
        FOREIGN KEY (report_id, org_id, project_id)
        REFERENCES app.analysis_reports (id, org_id, project_id),
    CONSTRAINT fk_analysis_report_versions_query_spec_version
        FOREIGN KEY (query_spec_version_id, org_id, project_id)
        REFERENCES app.query_spec_versions (id, org_id, project_id),
    CONSTRAINT fk_analysis_report_versions_query_spec
        FOREIGN KEY (query_spec_id, org_id, project_id)
        REFERENCES app.query_specs (id, org_id, project_id),
    CONSTRAINT fk_analysis_report_versions_predecessor
        FOREIGN KEY (predecessor_version_id, org_id, project_id)
        REFERENCES app.analysis_report_versions (id, org_id, project_id),
    CONSTRAINT ck_analysis_report_versions_number_positive CHECK (version_number >= 1),
    CONSTRAINT ck_analysis_report_versions_hash CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_analysis_report_versions_seed_is_object
        CHECK (jsonb_typeof(seed_provenance) = 'object'),
    CONSTRAINT ck_analysis_report_versions_lineage CHECK (
        (version_number = 1 AND predecessor_version_id IS NULL)
        OR (version_number > 1 AND predecessor_version_id IS NOT NULL)
    ),
    -- AC2 exactly: an accepted Template version, an accepted Spec version, or the
    -- exact literal. Never a null hiding as "to be decided", never 'deferred'.
    CONSTRAINT ck_analysis_report_versions_presentation_is_honest CHECK (
        (
            presentation_kind IN ('visualization_template_version', 'visualization_spec_version')
            AND app.is_exact_pin(presentation_version_id)
            AND presentation_absent_literal IS NULL
        )
        OR (
            presentation_kind IS NULL
            AND presentation_version_id IS NULL
            AND presentation_absent_literal = 'No accepted presentation contract'
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_analysis_report_versions_head
    ON app.analysis_report_versions (report_id, version_number DESC);
CREATE INDEX IF NOT EXISTS idx_analysis_report_versions_query_spec
    ON app.analysis_report_versions (query_spec_version_id);

ALTER TABLE app.analysis_reports
    DROP CONSTRAINT IF EXISTS fk_analysis_reports_current_version;
ALTER TABLE app.analysis_reports
    ADD CONSTRAINT fk_analysis_reports_current_version
    FOREIGN KEY (current_version_id, org_id, project_id)
    REFERENCES app.analysis_report_versions (id, org_id, project_id)
    DEFERRABLE INITIALLY DEFERRED;

-- ---------------------------------------------------------------------------
-- 2. Notebook: stable head, immutable composition versions, ordered blocks.
--
--    `app.notebooks` is NOT renamed into this. It keeps its rows and its readers.
--    `legacy_notebook_id` below is a one-way migration LINK so a canonical Notebook
--    can say which legacy row it descends from -- it is never read as content.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.analysis_notebooks (
    id                  TEXT PRIMARY KEY,
    org_id              TEXT NOT NULL,
    project_id          TEXT NOT NULL,
    label               TEXT NOT NULL,
    description         TEXT,
    current_version_id  TEXT,
    legacy_notebook_id  TEXT,
    archived_at         TIMESTAMPTZ,
    created_by          TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_analysis_notebooks_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT fk_analysis_notebooks_project
        FOREIGN KEY (org_id, project_id) REFERENCES app.projects (org_id, id),
    CONSTRAINT ck_analysis_notebooks_label_bounded
        CHECK (char_length(label) BETWEEN 1 AND 200),
    -- One canonical Notebook per legacy row at most, so a restartable backfill
    -- cannot produce two heads for the same history.
    CONSTRAINT uq_analysis_notebooks_legacy UNIQUE (legacy_notebook_id)
);

CREATE INDEX IF NOT EXISTS idx_analysis_notebooks_project
    ON app.analysis_notebooks (project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS app.analysis_notebook_versions (
    id                      TEXT PRIMARY KEY,
    notebook_id             TEXT NOT NULL,
    org_id                  TEXT NOT NULL,
    project_id              TEXT NOT NULL,
    version_number          INTEGER NOT NULL,
    label                   TEXT NOT NULL,
    content_hash            TEXT NOT NULL,
    predecessor_version_id  TEXT,
    created_by              TEXT NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_analysis_notebook_versions_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT uq_analysis_notebook_version_number UNIQUE (notebook_id, version_number),
    CONSTRAINT fk_analysis_notebook_versions_head
        FOREIGN KEY (notebook_id, org_id, project_id)
        REFERENCES app.analysis_notebooks (id, org_id, project_id),
    CONSTRAINT fk_analysis_notebook_versions_predecessor
        FOREIGN KEY (predecessor_version_id, org_id, project_id)
        REFERENCES app.analysis_notebook_versions (id, org_id, project_id),
    CONSTRAINT ck_analysis_notebook_versions_number_positive CHECK (version_number >= 1),
    CONSTRAINT ck_analysis_notebook_versions_hash CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_analysis_notebook_versions_lineage CHECK (
        (version_number = 1 AND predecessor_version_id IS NULL)
        OR (version_number > 1 AND predecessor_version_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_analysis_notebook_versions_head
    ON app.analysis_notebook_versions (notebook_id, version_number DESC);

ALTER TABLE app.analysis_notebooks
    DROP CONSTRAINT IF EXISTS fk_analysis_notebooks_current_version;
ALTER TABLE app.analysis_notebooks
    ADD CONSTRAINT fk_analysis_notebooks_current_version
    FOREIGN KEY (current_version_id, org_id, project_id)
    REFERENCES app.analysis_notebook_versions (id, org_id, project_id)
    DEFERRABLE INITIALLY DEFERRED;

-- A block is part of its version, so it is as immutable as the version. The
-- `block_key` is STABLE across versions -- that is what lets two Runs of two
-- versions be compared block by block instead of by position, which shifts.
CREATE TABLE IF NOT EXISTS app.analysis_notebook_version_blocks (
    id                          TEXT PRIMARY KEY,
    notebook_version_id         TEXT NOT NULL,
    notebook_id                 TEXT NOT NULL,
    org_id                      TEXT NOT NULL,
    project_id                  TEXT NOT NULL,
    block_key                   TEXT NOT NULL,
    position                    INTEGER NOT NULL,
    block_type                  TEXT NOT NULL,
    query_spec_version_id       TEXT,
    report_version_id           TEXT,
    presentation_kind           TEXT,
    presentation_version_id     TEXT,
    presentation_absent_literal TEXT,
    -- AC6: a block that is intentionally non-rendered says so HERE, in the
    -- composition, so its Run can record an explicit `No Render` instead of a gap
    -- that looks like a failure.
    renders                     BOOLEAN NOT NULL DEFAULT TRUE,
    -- AC5: the explicit current-versus-as-of rule, part of the block's meaning.
    as_of_rule                  TEXT NOT NULL DEFAULT 'current',
    narrative                   JSONB NOT NULL DEFAULT '{}'::jsonb,
    content_hash                TEXT NOT NULL,

    CONSTRAINT uq_analysis_notebook_blocks_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT uq_analysis_notebook_blocks_key UNIQUE (notebook_version_id, block_key),
    CONSTRAINT uq_analysis_notebook_blocks_position UNIQUE (notebook_version_id, position),
    CONSTRAINT fk_analysis_notebook_blocks_version
        FOREIGN KEY (notebook_version_id, org_id, project_id)
        REFERENCES app.analysis_notebook_versions (id, org_id, project_id),
    CONSTRAINT fk_analysis_notebook_blocks_notebook
        FOREIGN KEY (notebook_id, org_id, project_id)
        REFERENCES app.analysis_notebooks (id, org_id, project_id),
    CONSTRAINT fk_analysis_notebook_blocks_query_spec_version
        FOREIGN KEY (query_spec_version_id, org_id, project_id)
        REFERENCES app.query_spec_versions (id, org_id, project_id),
    CONSTRAINT fk_analysis_notebook_blocks_report_version
        FOREIGN KEY (report_version_id, org_id, project_id)
        REFERENCES app.analysis_report_versions (id, org_id, project_id),
    CONSTRAINT ck_analysis_notebook_blocks_position CHECK (position >= 1),
    CONSTRAINT ck_analysis_notebook_blocks_key_bounded
        CHECK (block_key ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
    CONSTRAINT ck_analysis_notebook_blocks_type
        CHECK (block_type IN ('query', 'report', 'narrative')),
    CONSTRAINT ck_analysis_notebook_blocks_as_of_rule
        CHECK (as_of_rule IN ('current', 'as_of')),
    CONSTRAINT ck_analysis_notebook_blocks_hash CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_analysis_notebook_blocks_narrative_is_object
        CHECK (jsonb_typeof(narrative) = 'object'),
    -- Exactly one analytical input, and only for an analytical block. A narrative
    -- block carrying a Query Spec version would be a second, invisible execution.
    CONSTRAINT ck_analysis_notebook_blocks_input CHECK (
        (block_type = 'query'
            AND query_spec_version_id IS NOT NULL AND report_version_id IS NULL)
        OR (block_type = 'report'
            AND report_version_id IS NOT NULL AND query_spec_version_id IS NULL)
        OR (block_type = 'narrative'
            AND query_spec_version_id IS NULL AND report_version_id IS NULL)
    ),
    -- A narrative block never renders a figure; saying it does would make the Run
    -- owe a Render nothing can produce.
    CONSTRAINT ck_analysis_notebook_blocks_narrative_does_not_render
        CHECK (block_type <> 'narrative' OR renders = FALSE),
    CONSTRAINT ck_analysis_notebook_blocks_presentation_is_honest CHECK (
        (
            presentation_kind IN ('visualization_template_version', 'visualization_spec_version')
            AND app.is_exact_pin(presentation_version_id)
            AND presentation_absent_literal IS NULL
        )
        OR (
            presentation_kind IS NULL
            AND presentation_version_id IS NULL
            AND presentation_absent_literal = 'No accepted presentation contract'
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_analysis_notebook_blocks_version
    ON app.analysis_notebook_version_blocks (notebook_version_id, position);

-- ---------------------------------------------------------------------------
-- 3. The canonical Render, with the complete replay contract.
--
--    Declared BEFORE the Run tables because both of them reference it.
--
--    THE TEN PINS of `visualization-and-rendering.md:318-322`, in the order that
--    document lists them, and the column that carries each:
--
--      1. Result identity ................ result_id (composite FK, migration 151)
--      2. retained Result data ........... result_content_hash + result_payload_retained
--      3. Visualization Spec version ..... visualization_spec_version_id
--      4. renderer build ................. renderer_adapter + renderer_build_id
--      5. runtime build .................. runtime_build_id
--      6. theme version .................. theme_version
--      7. formatter version .............. formatter_version
--      8. responsive profile ............. responsive_profile
--      9. local display state ............ display_state (bounded JSONB)
--     10. evidence manifest .............. evidence_manifest + datum_evidence_keys
--
--    plus creation context (creation_surface, created_by, created_at) which AC8
--    lists as its own bullet.
--
--    EVERY textual pin is NOT NULL and passes `app.is_exact_pin`, so this table can
--    hold no row at all until Stories 50.4 and 50.5 supply real identities. That is
--    the intended state today, and it is the honest one.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.renders (
    id                              TEXT PRIMARY KEY,
    org_id                          TEXT NOT NULL,
    project_id                      TEXT NOT NULL,

    -- 1 + 2: the exact Result, and the content hash of the retained payload. The
    -- hash is stored rather than derived so a Render can PROVE the evidence it was
    -- drawn from is the evidence still on disk.
    result_id                       TEXT NOT NULL,
    result_content_hash             TEXT NOT NULL,
    result_payload_retained         BOOLEAN NOT NULL DEFAULT TRUE,

    -- 3: Story 50.4 owns this identity. The composite foreign key is added by the
    -- DO block below the moment `app.visualization_spec_versions` exists; until
    -- then the NOT NULL + exact-pin CHECK is what refuses a placeholder.
    visualization_spec_version_id   TEXT NOT NULL,

    -- 4 + 5: Story 50.5 owns these. Same discipline.
    renderer_adapter                TEXT NOT NULL,
    renderer_build_id               TEXT NOT NULL,
    runtime_build_id                TEXT NOT NULL,

    -- 6 + 7 + 8
    theme_version                   TEXT NOT NULL,
    formatter_version               TEXT NOT NULL,
    responsive_profile              TEXT NOT NULL,

    -- 9: bounded local display state. It may hide or select rows already in the
    -- Result; it may not reaggregate. The bound is enforced, because "bounded" that
    -- nothing measures is a wish.
    display_state                   JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- 10: the evidence manifest and the datum/mark -> evidence-key mapping every
    -- tooltip, drill-through and feedback report resolves through.
    evidence_manifest               JSONB NOT NULL,
    datum_evidence_keys             JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Creation context.
    creation_surface                TEXT NOT NULL,
    origin_kind                     TEXT NOT NULL,
    origin_report_run_id            TEXT,
    origin_notebook_run_id          TEXT,
    predecessor_render_id           TEXT,
    content_hash                    TEXT NOT NULL,
    created_by                      TEXT NOT NULL,
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_renders_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT fk_renders_project
        FOREIGN KEY (org_id, project_id) REFERENCES app.projects (org_id, id),
    CONSTRAINT fk_renders_result
        FOREIGN KEY (result_id, org_id, project_id)
        REFERENCES app.query_results (id, org_id, project_id),
    CONSTRAINT fk_renders_predecessor
        FOREIGN KEY (predecessor_render_id, org_id, project_id)
        REFERENCES app.renders (id, org_id, project_id),
    CONSTRAINT ck_renders_hash CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_renders_result_hash CHECK (result_content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_renders_creation_surface
        CHECK (creation_surface IN ('explore', 'report', 'notebook', 'mcp')),
    CONSTRAINT ck_renders_origin_kind
        CHECK (origin_kind IN ('explore', 'report_run', 'notebook_run')),
    -- The origin reference matches the origin kind, or the Render claims a lineage
    -- it cannot show.
    CONSTRAINT ck_renders_origin_reference CHECK (
        (origin_kind = 'explore'
            AND origin_report_run_id IS NULL AND origin_notebook_run_id IS NULL)
        OR (origin_kind = 'report_run'
            AND origin_report_run_id IS NOT NULL AND origin_notebook_run_id IS NULL)
        OR (origin_kind = 'notebook_run'
            AND origin_notebook_run_id IS NOT NULL AND origin_report_run_id IS NULL)
    ),
    -- The ten pins, each refused the four placeholder words.
    CONSTRAINT ck_renders_pins_are_exact CHECK (
        app.is_exact_pin(visualization_spec_version_id)
        AND app.is_exact_pin(renderer_adapter)
        AND app.is_exact_pin(renderer_build_id)
        AND app.is_exact_pin(runtime_build_id)
        AND app.is_exact_pin(theme_version)
        AND app.is_exact_pin(formatter_version)
        AND app.is_exact_pin(responsive_profile)
    ),
    CONSTRAINT ck_renders_display_state_is_object
        CHECK (jsonb_typeof(display_state) = 'object'),
    CONSTRAINT ck_renders_display_state_bounded
        CHECK (pg_column_size(display_state) <= 65536),
    CONSTRAINT ck_renders_evidence_manifest_is_object
        CHECK (jsonb_typeof(evidence_manifest) = 'object'),
    -- An empty manifest is not a manifest. AC8 requires the mapping to exist, and a
    -- `{}` here would make every tooltip resolve to nothing while looking complete.
    CONSTRAINT ck_renders_evidence_manifest_not_empty
        CHECK (evidence_manifest <> '{}'::jsonb),
    CONSTRAINT ck_renders_datum_keys_is_object
        CHECK (jsonb_typeof(datum_evidence_keys) = 'object'),
    -- AC8: "A Render contains no live query instruction and grants no rerun
    -- authority." There is no column that could carry one, and this refuses one
    -- smuggled into display state.
    CONSTRAINT ck_renders_display_state_carries_no_query CHECK (
        NOT (display_state ?| ARRAY['query', 'query_spec', 'sql', 'rerun', 'refresh', 'tool_args'])
    )
);

CREATE INDEX IF NOT EXISTS idx_renders_project
    ON app.renders (project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_renders_result
    ON app.renders (result_id);
CREATE INDEX IF NOT EXISTS idx_renders_origin
    ON app.renders (project_id, origin_kind, created_at DESC);

-- Story 50.4 lands `app.visualization_spec_versions`. When it does, this migration
-- has already declared the intent; the constraint is added here so nobody has to
-- remember, and skipped silently while the table does not exist.
DO $$
BEGIN
    IF to_regclass('app.visualization_spec_versions') IS NOT NULL THEN
        BEGIN
            ALTER TABLE app.renders
                DROP CONSTRAINT IF EXISTS fk_renders_visualization_spec_version;
            ALTER TABLE app.renders
                ADD CONSTRAINT fk_renders_visualization_spec_version
                FOREIGN KEY (visualization_spec_version_id, org_id, project_id)
                REFERENCES app.visualization_spec_versions (id, org_id, project_id);
        EXCEPTION WHEN undefined_object OR undefined_column THEN
            -- Story 50.4 shaped its scope key differently. It owns the constraint;
            -- leaving the exact-pin CHECK in place is the honest fallback.
            NULL;
        END;
    END IF;
END $$;

-- Retention is a SEPARATE, audited lifecycle action -- never an UPDATE on the
-- Render and never inline best-effort cleanup (AC8). Recording the intent in its
-- own append-only ledger is what makes "expired" distinguishable from "was never
-- there", which the current `app.render_snapshots` inline purge cannot do.
CREATE TABLE IF NOT EXISTS app.render_retention_actions (
    id              TEXT PRIMARY KEY,
    render_id       TEXT NOT NULL,
    org_id          TEXT NOT NULL,
    project_id      TEXT NOT NULL,
    action          TEXT NOT NULL,
    reason          TEXT NOT NULL,
    policy_ref      TEXT NOT NULL,
    actor           TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_render_retention_actions_render
        FOREIGN KEY (render_id, org_id, project_id)
        REFERENCES app.renders (id, org_id, project_id),
    CONSTRAINT ck_render_retention_actions_action
        CHECK (action IN ('retention_expired', 'runtime_unavailable', 'erasure_requested'))
);

CREATE INDEX IF NOT EXISTS idx_render_retention_actions_render
    ON app.render_retention_actions (render_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- 4. Report runs: immutable execution references.
--
--    A Report run is NOT the Result. The Result is Story 50.1's row; this table
--    records that THIS Report version was the reason it exists, with the actor and
--    the as-of context requested and resolved (AC3).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.analysis_report_runs (
    id                      TEXT PRIMARY KEY,
    report_id               TEXT NOT NULL,
    report_version_id       TEXT NOT NULL,
    org_id                  TEXT NOT NULL,
    project_id              TEXT NOT NULL,
    query_spec_version_id   TEXT NOT NULL,
    result_id               TEXT NOT NULL,
    render_id               TEXT,
    outcome                 TEXT NOT NULL,
    requested_as_of         TEXT,
    resolved_as_of          TEXT,
    actor                   TEXT NOT NULL,
    started_at              TIMESTAMPTZ NOT NULL,
    ended_at                TIMESTAMPTZ NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_analysis_report_runs_scope UNIQUE (id, org_id, project_id),
    -- One run reference per Result: a Result belongs to exactly one reason.
    CONSTRAINT uq_analysis_report_runs_result UNIQUE (result_id),
    CONSTRAINT fk_analysis_report_runs_version
        FOREIGN KEY (report_version_id, org_id, project_id)
        REFERENCES app.analysis_report_versions (id, org_id, project_id),
    CONSTRAINT fk_analysis_report_runs_report
        FOREIGN KEY (report_id, org_id, project_id)
        REFERENCES app.analysis_reports (id, org_id, project_id),
    CONSTRAINT fk_analysis_report_runs_result
        FOREIGN KEY (result_id, org_id, project_id)
        REFERENCES app.query_results (id, org_id, project_id),
    CONSTRAINT fk_analysis_report_runs_query_spec_version
        FOREIGN KEY (query_spec_version_id, org_id, project_id)
        REFERENCES app.query_spec_versions (id, org_id, project_id),
    CONSTRAINT fk_analysis_report_runs_render
        FOREIGN KEY (render_id, org_id, project_id)
        REFERENCES app.renders (id, org_id, project_id),
    CONSTRAINT ck_analysis_report_runs_outcome
        CHECK (outcome IN ('success', 'empty', 'degraded', 'refused', 'unavailable')),
    CONSTRAINT ck_analysis_report_runs_ends_after_start CHECK (ended_at >= started_at)
);

CREATE INDEX IF NOT EXISTS idx_analysis_report_runs_report
    ON app.analysis_report_runs (report_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_analysis_report_runs_version
    ON app.analysis_report_runs (report_version_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- 5. Notebook Runs, and their per-block outcomes.
--
--    The Run HEAD advances forward only, exactly like migration 151's execution
--    attempt: `accepted` -> terminal. It may never be rebound to another Notebook
--    version, and a terminal Run may never be reopened -- that is what makes AC7's
--    "retry returns the ORIGINAL run" enforceable rather than hoped for.
--
--    `idempotency_key` is UNIQUE per Notebook. A scheduler that fires twice for the
--    same due window presents the same key and gets the same Run back.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.analysis_notebook_runs (
    id                      TEXT PRIMARY KEY,
    notebook_id             TEXT NOT NULL,
    notebook_version_id     TEXT NOT NULL,
    org_id                  TEXT NOT NULL,
    project_id              TEXT NOT NULL,
    idempotency_key         TEXT NOT NULL,
    dispatch_source         TEXT NOT NULL,
    state                   TEXT NOT NULL DEFAULT 'accepted',
    outcome                 TEXT,
    requested_as_of         TEXT,
    resolved_as_of          TEXT,
    actor                   TEXT NOT NULL,
    accepted_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    terminal_at             TIMESTAMPTZ,

    CONSTRAINT uq_analysis_notebook_runs_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT uq_analysis_notebook_runs_idempotency
        UNIQUE (notebook_id, idempotency_key),
    CONSTRAINT fk_analysis_notebook_runs_version
        FOREIGN KEY (notebook_version_id, org_id, project_id)
        REFERENCES app.analysis_notebook_versions (id, org_id, project_id),
    CONSTRAINT fk_analysis_notebook_runs_notebook
        FOREIGN KEY (notebook_id, org_id, project_id)
        REFERENCES app.analysis_notebooks (id, org_id, project_id),
    CONSTRAINT ck_analysis_notebook_runs_dispatch
        CHECK (dispatch_source IN ('manual', 'scheduled')),
    CONSTRAINT ck_analysis_notebook_runs_state
        CHECK (state IN ('accepted', 'running', 'terminal')),
    -- A terminal Run states its outcome; a running one does not pretend to have one.
    CONSTRAINT ck_analysis_notebook_runs_terminal_is_complete CHECK (
        (state = 'terminal' AND terminal_at IS NOT NULL
            AND outcome IN ('succeeded', 'partial', 'failed'))
        OR (state <> 'terminal' AND terminal_at IS NULL AND outcome IS NULL)
    ),
    CONSTRAINT ck_analysis_notebook_runs_ends_after_start
        CHECK (terminal_at IS NULL OR terminal_at >= accepted_at)
);

CREATE INDEX IF NOT EXISTS idx_analysis_notebook_runs_notebook
    ON app.analysis_notebook_runs (notebook_id, accepted_at DESC);
CREATE INDEX IF NOT EXISTS idx_analysis_notebook_runs_open
    ON app.analysis_notebook_runs (project_id, accepted_at)
    WHERE terminal_at IS NULL;

-- One row per block per Run, inserted once. AC6 forbids a summary, a pull-id list,
-- a mutable `latest` pointer and `envelope_ref='deferred'` -- so this table has a
-- column for the exact Result and the exact Render, and a spelled-out literal for a
-- block whose accepted contract is intentionally non-rendered.
CREATE TABLE IF NOT EXISTS app.analysis_notebook_run_blocks (
    id                      TEXT PRIMARY KEY,
    run_id                  TEXT NOT NULL,
    org_id                  TEXT NOT NULL,
    project_id              TEXT NOT NULL,
    block_key               TEXT NOT NULL,
    position                INTEGER NOT NULL,
    block_type              TEXT NOT NULL,
    query_spec_version_id   TEXT,
    report_version_id       TEXT,
    result_id               TEXT,
    render_id               TEXT,
    render_absent_literal   TEXT,
    status                  TEXT NOT NULL,
    limitation              TEXT,
    resolved_as_of          TEXT,
    started_at              TIMESTAMPTZ NOT NULL,
    ended_at                TIMESTAMPTZ NOT NULL,

    CONSTRAINT uq_analysis_notebook_run_blocks_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT uq_analysis_notebook_run_blocks_key UNIQUE (run_id, block_key),
    CONSTRAINT fk_analysis_notebook_run_blocks_run
        FOREIGN KEY (run_id, org_id, project_id)
        REFERENCES app.analysis_notebook_runs (id, org_id, project_id),
    CONSTRAINT fk_analysis_notebook_run_blocks_result
        FOREIGN KEY (result_id, org_id, project_id)
        REFERENCES app.query_results (id, org_id, project_id),
    CONSTRAINT fk_analysis_notebook_run_blocks_render
        FOREIGN KEY (render_id, org_id, project_id)
        REFERENCES app.renders (id, org_id, project_id),
    CONSTRAINT fk_analysis_notebook_run_blocks_query_spec_version
        FOREIGN KEY (query_spec_version_id, org_id, project_id)
        REFERENCES app.query_spec_versions (id, org_id, project_id),
    CONSTRAINT fk_analysis_notebook_run_blocks_report_version
        FOREIGN KEY (report_version_id, org_id, project_id)
        REFERENCES app.analysis_report_versions (id, org_id, project_id),
    CONSTRAINT ck_analysis_notebook_run_blocks_position CHECK (position >= 1),
    CONSTRAINT ck_analysis_notebook_run_blocks_type
        CHECK (block_type IN ('query', 'report', 'narrative')),
    CONSTRAINT ck_analysis_notebook_run_blocks_status
        CHECK (status IN ('succeeded', 'empty', 'degraded', 'refused', 'unavailable', 'failed')),
    CONSTRAINT ck_analysis_notebook_run_blocks_ends_after_start
        CHECK (ended_at >= started_at),
    -- An analytical block that SUCCEEDED names its Result. One that did not says
    -- why in `limitation` and names none -- it never points at an older Result.
    CONSTRAINT ck_analysis_notebook_run_blocks_result_matches_status CHECK (
        block_type = 'narrative'
        OR (status IN ('succeeded', 'empty', 'degraded', 'refused', 'unavailable')
            AND result_id IS NOT NULL)
        OR (status = 'failed' AND result_id IS NULL AND limitation IS NOT NULL)
    ),
    CONSTRAINT ck_analysis_notebook_run_blocks_narrative_has_no_result
        CHECK (block_type <> 'narrative' OR result_id IS NULL),
    -- AC6, exactly: a Render identity, or the exact literal. Never both, never
    -- neither, and never a null standing in for "we did not get round to it".
    CONSTRAINT ck_analysis_notebook_run_blocks_render_is_honest CHECK (
        (render_id IS NOT NULL AND render_absent_literal IS NULL)
        OR (render_id IS NULL AND render_absent_literal IN (
                'No Render',
                'No Render: block failed',
                'No Render: no accepted presentation contract'
            ))
    )
);

CREATE INDEX IF NOT EXISTS idx_analysis_notebook_run_blocks_run
    ON app.analysis_notebook_run_blocks (run_id, position);

-- Operational scheduling policy for a Notebook. AC7: this is CONFIGURATION, not
-- content and not evidence, so it lives in its own mutable table and editing it
-- never touches a version or a Run.
CREATE TABLE IF NOT EXISTS app.analysis_notebook_schedules (
    notebook_id     TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL,
    project_id      TEXT NOT NULL,
    recurrence      TEXT NOT NULL,
    timezone        TEXT NOT NULL DEFAULT 'UTC',
    enabled         BOOLEAN NOT NULL DEFAULT FALSE,
    next_due_at     TIMESTAMPTZ,
    updated_by      TEXT NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_analysis_notebook_schedules_notebook
        FOREIGN KEY (notebook_id, org_id, project_id)
        REFERENCES app.analysis_notebooks (id, org_id, project_id),
    CONSTRAINT ck_analysis_notebook_schedules_recurrence
        CHECK (recurrence IN ('daily', 'weekly', 'monthly'))
);

CREATE INDEX IF NOT EXISTS idx_analysis_notebook_schedules_due
    ON app.analysis_notebook_schedules (next_due_at)
    WHERE enabled;

-- ---------------------------------------------------------------------------
-- 6. Immutability, in the database, where it cannot be argued with.
--
--    Reuses migration 151's `app.reject_analytical_evidence_mutation()` so both
--    stories raise the same SQLSTATE and the same sentence. The `app.rgpd_erasure`
--    escape hatch is the same one migrations 098/099, 150 and 151 use: erasure is
--    the only legitimate reason these rows disappear, and it is explicit.
-- ---------------------------------------------------------------------------
DROP TRIGGER IF EXISTS trg_analysis_report_versions_immutable
    ON app.analysis_report_versions;
CREATE TRIGGER trg_analysis_report_versions_immutable
BEFORE UPDATE OR DELETE ON app.analysis_report_versions
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_analytical_evidence_mutation();

DROP TRIGGER IF EXISTS trg_analysis_report_runs_immutable ON app.analysis_report_runs;
CREATE TRIGGER trg_analysis_report_runs_immutable
BEFORE UPDATE OR DELETE ON app.analysis_report_runs
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_analytical_evidence_mutation();

DROP TRIGGER IF EXISTS trg_analysis_notebook_versions_immutable
    ON app.analysis_notebook_versions;
CREATE TRIGGER trg_analysis_notebook_versions_immutable
BEFORE UPDATE OR DELETE ON app.analysis_notebook_versions
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_analytical_evidence_mutation();

DROP TRIGGER IF EXISTS trg_analysis_notebook_version_blocks_immutable
    ON app.analysis_notebook_version_blocks;
CREATE TRIGGER trg_analysis_notebook_version_blocks_immutable
BEFORE UPDATE OR DELETE ON app.analysis_notebook_version_blocks
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_analytical_evidence_mutation();

DROP TRIGGER IF EXISTS trg_analysis_notebook_run_blocks_immutable
    ON app.analysis_notebook_run_blocks;
CREATE TRIGGER trg_analysis_notebook_run_blocks_immutable
BEFORE UPDATE OR DELETE ON app.analysis_notebook_run_blocks
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_analytical_evidence_mutation();

DROP TRIGGER IF EXISTS trg_renders_immutable ON app.renders;
CREATE TRIGGER trg_renders_immutable
BEFORE UPDATE OR DELETE ON app.renders
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_analytical_evidence_mutation();

DROP TRIGGER IF EXISTS trg_render_retention_actions_append_only
    ON app.render_retention_actions;
CREATE TRIGGER trg_render_retention_actions_append_only
BEFORE UPDATE OR DELETE ON app.render_retention_actions
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_analytical_evidence_mutation();

DROP TRIGGER IF EXISTS trg_renders_block_truncate ON app.renders;
CREATE TRIGGER trg_renders_block_truncate
BEFORE TRUNCATE ON app.renders
FOR EACH STATEMENT EXECUTE FUNCTION app.reject_analytical_truncate();

DROP TRIGGER IF EXISTS trg_analysis_report_versions_block_truncate
    ON app.analysis_report_versions;
CREATE TRIGGER trg_analysis_report_versions_block_truncate
BEFORE TRUNCATE ON app.analysis_report_versions
FOR EACH STATEMENT EXECUTE FUNCTION app.reject_analytical_truncate();

DROP TRIGGER IF EXISTS trg_analysis_notebook_run_blocks_block_truncate
    ON app.analysis_notebook_run_blocks;
CREATE TRIGGER trg_analysis_notebook_run_blocks_block_truncate
BEFORE TRUNCATE ON app.analysis_notebook_run_blocks
FOR EACH STATEMENT EXECUTE FUNCTION app.reject_analytical_truncate();

-- The Notebook Run head: forward only, never rebound, never reopened.
CREATE OR REPLACE FUNCTION app.reject_notebook_run_rewrite()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'an accepted Notebook Run cannot be deleted'
            USING ERRCODE = '23000';
    END IF;

    IF OLD.state = 'terminal' THEN
        RAISE EXCEPTION 'a terminal Notebook Run cannot be reopened: rerun creates a new Run'
            USING ERRCODE = '23000';
    END IF;

    IF NEW.id <> OLD.id
       OR NEW.org_id <> OLD.org_id
       OR NEW.project_id <> OLD.project_id
       OR NEW.notebook_id <> OLD.notebook_id
       OR NEW.notebook_version_id <> OLD.notebook_version_id
       OR NEW.idempotency_key <> OLD.idempotency_key
       OR NEW.accepted_at <> OLD.accepted_at
    THEN
        RAISE EXCEPTION 'a Notebook Run may not be rebound to another Notebook version'
            USING ERRCODE = '23000';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_analysis_notebook_runs_forward_only ON app.analysis_notebook_runs;
CREATE TRIGGER trg_analysis_notebook_runs_forward_only
BEFORE UPDATE OR DELETE ON app.analysis_notebook_runs
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_notebook_run_rewrite();

-- A stable head may advance its pointer and be archived. It may NOT change Project,
-- and it may not walk its pointer BACKWARDS onto an older version -- AC11's "no
-- action silently rebinds a prior version to current". Enforced here because the
-- pointer is the one mutable thing in the whole model.
CREATE OR REPLACE FUNCTION app.reject_stable_head_rebind()
RETURNS TRIGGER AS $$
DECLARE
    old_number INTEGER;
    new_number INTEGER;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'archive % rather than deleting it: its versions and runs are evidence',
            TG_TABLE_NAME USING ERRCODE = '23000';
    END IF;

    IF NEW.id <> OLD.id OR NEW.org_id <> OLD.org_id OR NEW.project_id <> OLD.project_id THEN
        RAISE EXCEPTION 'a % may not change identity or Project', TG_TABLE_NAME
            USING ERRCODE = '23000';
    END IF;

    IF NEW.current_version_id IS DISTINCT FROM OLD.current_version_id
       AND OLD.current_version_id IS NOT NULL THEN
        IF TG_TABLE_NAME = 'analysis_reports' THEN
            SELECT version_number INTO old_number FROM app.analysis_report_versions
                WHERE id = OLD.current_version_id;
            SELECT version_number INTO new_number FROM app.analysis_report_versions
                WHERE id = NEW.current_version_id;
        ELSE
            SELECT version_number INTO old_number FROM app.analysis_notebook_versions
                WHERE id = OLD.current_version_id;
            SELECT version_number INTO new_number FROM app.analysis_notebook_versions
                WHERE id = NEW.current_version_id;
        END IF;
        IF new_number IS NULL OR old_number IS NULL OR new_number <= old_number THEN
            RAISE EXCEPTION
                'a stable head advances only to a NEWER version (% -> %)', old_number, new_number
                USING ERRCODE = '23000';
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_analysis_reports_forward_only ON app.analysis_reports;
CREATE TRIGGER trg_analysis_reports_forward_only
BEFORE UPDATE OR DELETE ON app.analysis_reports
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_stable_head_rebind();

DROP TRIGGER IF EXISTS trg_analysis_notebooks_forward_only ON app.analysis_notebooks;
CREATE TRIGGER trg_analysis_notebooks_forward_only
BEFORE UPDATE OR DELETE ON app.analysis_notebooks
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_stable_head_rebind();

-- ---------------------------------------------------------------------------
-- 7. Fail-closed Row Level Security.
--
--    Same contract as migrations 149, 150 and 151: RLS enabled with no applicable
--    policy exposes no rows, FORCE applies it to the table owner too, and the
--    application authorization check still runs first. RLS is the floor, not the
--    door (AC13).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'analysis_reports',
        'analysis_report_versions',
        'analysis_report_runs',
        'analysis_notebooks',
        'analysis_notebook_versions',
        'analysis_notebook_version_blocks',
        'analysis_notebook_runs',
        'analysis_notebook_run_blocks',
        'analysis_notebook_schedules',
        'renders',
        'render_retention_actions'
    ] LOOP
        EXECUTE format('ALTER TABLE app.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE app.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON app.%I', t || '_strict', t);
        EXECUTE format(
            'CREATE POLICY %I ON app.%I '
            'USING (current_setting(''toorow.enforce_epic36'', true) IS DISTINCT FROM ''on'' '
            '       OR app.epic36_has_resource_access(org_id, ''project'', project_id)) '
            'WITH CHECK (current_setting(''toorow.enforce_epic36'', true) IS DISTINCT FROM ''on'' '
            '       OR app.epic36_has_resource_access(org_id, ''project'', project_id))',
            t || '_strict', t
        );
    END LOOP;
END $$;

COMMIT;
