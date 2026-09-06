-- Story 50.5 corrective: `array_length(col, 1) >= 1` is a NULL-satisfiable CHECK,
-- and it is the same class migrations 155 and 159 already closed twice.
--
-- THE DEFECT, in one line of SQL.
--
--     SELECT array_length('{}'::text[], 1) >= 1;   -->  NULL
--
-- `array_length` returns NULL rather than 0 for an empty array, because an empty
-- array has no dimension 1. A CHECK rejects only FALSE, so `NULL` is ACCEPTED --
-- and in a conjunction it poisons the whole expression: `NULL AND true` is NULL,
-- so `ck_renderer_runtime_builds_profiles` accepted a build declaring NO
-- responsive profile at all, which is the one thing it exists to require.
--
-- PROVED, NOT ARGUED, on PostgreSQL 17.2 with the ordinary `connector` role
-- before this file was written:
--
--     INSERT INTO app.renderer_runtime_builds
--       (id, runtime_build, family, renderer_id, theme_version, formatter_version,
--        responsive_profiles, git_sha)
--     VALUES ('bar/f2probe@1.0.0', '@toorow/card-shell/viz@0.1.0+abcdef1', 'bar',
--             'f2probe', 'viz-theme@1', 'viz-formatters@1', '{}'::text[], 'abcdef1');
--     -- ACCEPTED (1 row)
--
-- TWO CONSTRAINTS, NOT ONE, BECAUSE THE CLASS IS WHAT IS BEING REPAIRED.
-- Migration 160 (Story 50.5) wrote one of them. The sweep that was supposed to
-- make this class visible -- `server/tests/core/test_check_constraints_null_proof_pg.py`,
-- landed with 159 -- could not see either, and that is the deeper finding: its
-- candidate generator reduced an array type to its element type, so every value
-- it probed a `text[]` column with (`'console'::text[]`, `'__no_such_value__'::text[]`)
-- raised `malformed array literal`, was caught, and was skipped. Every candidate
-- failing to cast is indistinguishable from no candidate defeating the
-- constraint, so the sweep reported both as clean. It now generates array-shaped
-- candidates -- `'{}'` first -- and with that one change it immediately reported
-- BOTH constraints below, including one that predates Story 50.5 entirely.
-- Repairing only the one this story wrote would have left the sweep's own hole
-- open and the older constraint defeated.
--
-- THE FIX is `cardinality()`, which returns 0 for an empty array and never NULL
-- for a NOT NULL column. It is preferred here over `COALESCE(array_length(...), 0)`
-- because it states the intent directly and cannot be re-derived wrongly by the
-- next reader. Both columns are NOT NULL, so `cardinality` is total on them.
--
-- NOTHING LEGITIMATE CHANGES. Measured on this database before writing: 0 rows in
-- `app.renderer_runtime_builds` and 0 rows in `app.golden_question_versions`
-- would violate the tightened form. A row that satisfied the old constraint by
-- being TRUE still satisfies this one; only the rows that satisfied it by being
-- NULL -- which is to say, by carrying nothing -- are now refused.
--
-- MIGRATIONS 160 AND 130 ARE NOT EDITED. An applied migration is never re-edited;
-- it is corrected by the next one. This is that one.

BEGIN;

-- 1. Story 50.5, migration 160. A renderer build declares at least one responsive
--    profile it supports. An empty array said "supports nothing" and was taken.
ALTER TABLE app.renderer_runtime_builds
    DROP CONSTRAINT IF EXISTS ck_renderer_runtime_builds_profiles;
ALTER TABLE app.renderer_runtime_builds
    ADD CONSTRAINT ck_renderer_runtime_builds_profiles CHECK (
        cardinality(responsive_profiles) >= 1
        AND responsive_profiles <@ ARRAY['console', 'mcp-inline', 'mcp-fullscreen', 'share']::TEXT[]
    );

-- 2. The same class, found by the repaired sweep, on a table this story does not
--    own and did not write. A golden question version that names no capability is
--    a version nothing can be routed by; the constraint already said so and was
--    simply not enforcing it.
ALTER TABLE app.golden_question_versions
    DROP CONSTRAINT IF EXISTS ck_golden_question_versions_capabilities;
ALTER TABLE app.golden_question_versions
    ADD CONSTRAINT ck_golden_question_versions_capabilities CHECK (
        cardinality(capability_tags) >= 1
    );

-- The `runtime_build` suffix is a CONTENT HASH of the runtime sources, not a git
-- SHA. Migration 160's column comment implied otherwise, and the implication was
-- load-bearing: a generator that runs before a commit can only ever record the
-- PREVIOUS commit, so the shipped identity named a tree containing no `viz/` at
-- all while the replay refusal told the reader to check that build out. The shape
-- CHECK is unchanged -- twelve hex characters satisfy `[0-9a-f]{7,40}` either way
-- -- so this is a correction to what the column MEANS, recorded where a reader
-- will find it rather than left to be rediscovered.
COMMENT ON COLUMN app.renderer_runtime_builds.runtime_build IS
    '@toorow/card-shell/viz@<semver>+<content-hash>. The suffix is a SHA-256 '
    'prefix over ui/cards/shell/src/viz/** (ui/cards/shell/scripts/runtime-identity.mjs), '
    'so it names the bytes that drew the chart. It is NOT a git SHA: a build '
    'identity generated before its own commit can only name the previous one.';

COMMENT ON COLUMN app.renderer_runtime_builds.git_sha IS
    'The commit the deployment was built from. Informational: it is NOT the '
    'suffix of runtime_build, and nothing cross-checks the two, because the two '
    'answer different questions -- which code drew this, and what was deployed.';

COMMIT;
