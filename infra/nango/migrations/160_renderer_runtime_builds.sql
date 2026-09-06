-- Story 50.5: the renderer/runtime BUILD IDENTITY ledger -- the last missing link
-- of the Render replay contract.
--
-- WHY THIS FILE EXISTS, MEASURED RATHER THAN ASSUMED.
-- `server/core/analyze_artifacts.py:84` names `app.renderer_runtime_builds` as the
-- registry Story 50.5 owes, and `render_contract_state()` reports it missing on
-- every deployment of this repository. While it is missing, `create_render()`
-- refuses EVERY Render with `render_contract_unavailable` -- correctly, because a
-- Render that could not name the build that drew it is not replayable. Story 50.4
-- landed the other half (`app.visualization_spec_versions`, migration 156), so
-- after this file the refusal stops being reached and the pins become the thing
-- that is checked.
--
-- THE NUMBER. 156 (50.4) and 157 were on disk; 158 and 159 were taken by two
-- sessions running beside this one when it was written; 160 is the first free
-- number, verified with `ls infra/nango/migrations/` and
-- `scripts/check_migration_catalog.py` on the day, never from a plan.
--
-- WHAT THIS IS *NOT*, and the distinction is the whole architecture.
--
--   * It is NOT the renderer registry. Which renderer serves which visual family,
--     with what field and volume constraints and what responsive capabilities, is
--     a BUILD-TIME CODE module (`ui/cards/shell/src/viz/registry.ts`). It has to
--     be: `ARCHITECTURE-SPINE.md:51` (AD-2) forbids executable presentation
--     metadata resolved at runtime in the same sentence that forbids a
--     connector-owned standard renderer, and a registry row a connector could
--     write would be exactly that escape hatch.
--   * It IS the ledger of build IDENTITIES this deployment ships, so a retained
--     Render's `renderer_build_id` and `runtime_build_id` point at something a
--     human can check out. That is reference data about our own artifacts, not a
--     rule anyone can execute.
--
-- WHY IT CARRIES NO org_id AND NO project_id. A build identity is a property of
-- the deployed code, identical for every organization. Scoping it per Project
-- would invite two Projects to disagree about what `toorow-echarts-bar@1.0.0`
-- drew, which is the opposite of a replay pin.
--
-- IMMUTABLE, LIKE EVERY OTHER PIN TARGET. A build identity that could be edited
-- would let a Render's pin silently start meaning something else. The trigger
-- reuses `app.reject_analytical_evidence_mutation()` from migration 151 rather
-- than declaring a second immutability function -- two functions is two policies,
-- and the second one drifts.

CREATE TABLE IF NOT EXISTS app.renderer_runtime_builds (
    -- `<family>/<renderer_id>@<semver>` -- exactly what a Render pins in
    -- `app.renders.renderer_build_id`.
    id                      TEXT PRIMARY KEY,
    -- `@toorow/card-shell/viz@<semver>+<git-short-sha>`. The `/viz` segment
    -- matters: the runtime is a subtree of a package whose card primitives
    -- version independently, so a pin naming the package alone would not
    -- identify the build that drew the chart.
    runtime_build           TEXT NOT NULL,
    family                  TEXT NOT NULL,
    renderer_id             TEXT NOT NULL,
    theme_version           TEXT NOT NULL,
    formatter_version       TEXT NOT NULL,
    -- The responsive profiles this renderer build supports. Values come from
    -- Story 50.4's enum, mirrored by the CHECK below so a direct SQL insert
    -- cannot invent a fifth profile either.
    responsive_profiles     TEXT[] NOT NULL,
    -- Which git commit produced the runtime bundle. Redundant with the suffix of
    -- `runtime_build` on purpose: a reader looking for "what do I check out"
    -- should not have to parse a string.
    git_sha                 TEXT NOT NULL,
    registered_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Every identity is EXACT. `app.is_exact_pin` (migration 154) refuses the
    -- placeholder words `legacy`, `current`, `deferred`, `latest`, `unknown` and
    -- `none`, which is the same guard `app.renders` applies to the pins that
    -- point here -- one rule, stated in one function, enforced at both ends.
    CONSTRAINT ck_renderer_runtime_builds_exact CHECK (
        app.is_exact_pin(id)
        AND app.is_exact_pin(runtime_build)
        AND app.is_exact_pin(theme_version)
        AND app.is_exact_pin(formatter_version)
        AND app.is_exact_pin(git_sha)
    ),
    -- The two identity SHAPES, so a malformed pin cannot enter through psql.
    CONSTRAINT ck_renderer_runtime_builds_id_shape CHECK (
        id ~ '^[a-z0-9_]+/[a-z0-9-]+@[0-9]+\.[0-9]+\.[0-9]+$'
    ),
    CONSTRAINT ck_renderer_runtime_builds_runtime_shape CHECK (
        runtime_build ~ '^@toorow/card-shell/viz@[0-9]+\.[0-9]+\.[0-9]+\+[0-9a-f]{7,40}$'
    ),
    -- The family vocabulary is closed by migration 156 for a Visualization Spec;
    -- it is closed here too, because a build claiming to draw a family the
    -- grammar cannot express would be unreachable by construction.
    CONSTRAINT ck_renderer_runtime_builds_family CHECK (
        family IN ('table', 'kpi', 'line', 'area', 'bar', 'stacked_bar', 'scatter')
    ),
    -- Story 50.4's responsive-profile enum, mirrored. There is no `compact`:
    -- compaction is a behaviour a profile may exhibit, not a fifth name.
    CONSTRAINT ck_renderer_runtime_builds_profiles CHECK (
        array_length(responsive_profiles, 1) >= 1
        AND responsive_profiles <@ ARRAY['console', 'mcp-inline', 'mcp-fullscreen', 'share']::TEXT[]
    ),
    -- The id must actually be built from its own parts. Without this, a row could
    -- say it is `bar/x@1.0.0` while claiming family `line`, and the ledger would
    -- be a place where two answers to the same question live.
    CONSTRAINT ck_renderer_runtime_builds_id_matches_parts CHECK (
        id LIKE family || '/' || renderer_id || '@%'
    )
);

COMMENT ON TABLE app.renderer_runtime_builds IS
    'Story 50.5 -- the ledger of renderer and runtime BUILD IDENTITIES this '
    'deployment ships, so a retained Render can be replayed by the exact build '
    'that drew it. It is not the renderer registry: which renderer serves which '
    'family is build-time code (ui/cards/shell/src/viz/registry.ts), because AD-2 '
    'forbids executable presentation metadata resolved at runtime.';

CREATE INDEX IF NOT EXISTS idx_renderer_runtime_builds_runtime
    ON app.renderer_runtime_builds (runtime_build);
CREATE INDEX IF NOT EXISTS idx_renderer_runtime_builds_family
    ON app.renderer_runtime_builds (family);

-- Insert-once. A build identity never changes meaning after a Render pinned it.
DROP TRIGGER IF EXISTS trg_renderer_runtime_builds_immutable ON app.renderer_runtime_builds;
CREATE TRIGGER trg_renderer_runtime_builds_immutable
    BEFORE UPDATE OR DELETE ON app.renderer_runtime_builds
    FOR EACH ROW EXECUTE FUNCTION app.reject_analytical_evidence_mutation();

-- RLS is enabled and the read policy is permissive BY DECISION, not by omission.
-- This table holds no tenant data: it describes our own deployed artifacts. A
-- per-org policy here would let one organization's Render fail to resolve a build
-- another organization can see, which is a replay failure with a tenancy
-- explanation and no tenancy cause. Writes remain impossible after insert through
-- the immutability trigger above.
ALTER TABLE app.renderer_runtime_builds ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS renderer_runtime_builds_readable ON app.renderer_runtime_builds;
CREATE POLICY renderer_runtime_builds_readable ON app.renderer_runtime_builds
    USING (true)
    WITH CHECK (true);
