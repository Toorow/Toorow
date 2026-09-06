-- 304 -- Picture-in-picture is a profile the product HAS, not one it promises.
--
-- RATIFIED TARGET, unchanged since it was written: the Surface parity table of
-- `docs/product-architecture/visualization-and-rendering.md:346` gives the MCP
-- App layout as "Inline/fullscreen/PiP profile when supported". "When supported"
-- qualifies the HOST's capability, never ours. Picture-in-picture is therefore a
-- mode of the target, and the delivered vocabulary shipped four values without
-- it -- recorded as criterion [14] of that surface, verdict `true`, on
-- 2026-08-24.
--
-- THE DECISION TAKEN, 2026-08-24: honour the table as it stands and BUILD the
-- mode, rather than amend the table to withdraw it. So `mcp-pip` enters
-- `server/core/visualization_specs.RESPONSIVE_PROFILES`, the runtime grows a
-- layout branch for it (`ui/cards/shell/src/viz/responsive.ts`), and this
-- migration widens the one database CHECK that mirrors that tuple.
--
-- WHY THE CHECK HAS TO MOVE IN THE SAME BREATH. `app.renderer_runtime_builds`
-- rows are a PROJECTION of the shipped renderer registry
-- (`scripts/register_renderer_builds.py`, reading the committed manifest
-- `ui/cards/shell/src/viz/rendererBuilds.generated.json`). Every renderer
-- declares the whole profile vocabulary it supports, so the moment `mcp-pip`
-- joins the tuple the emitted manifest carries five values per build, and the
-- projection would be refused by `responsive_profiles <@ ARRAY[... four ...]`.
-- A vocabulary the code has and the database refuses is the same drift read from
-- the other side.
--
-- IT IS A WIDENING AND NOTHING ELSE. `<@` is containment: every array that
-- satisfied the four-value form satisfies the five-value one, so no existing row
-- can be made invalid by this file and no row is rewritten. The `cardinality >= 1`
-- half that migration 165 repaired is carried over untouched -- a build declaring
-- no profile at all is still refused, which is the one thing the constraint
-- exists to require.
--
-- MIGRATIONS 160 AND 165 ARE NOT EDITED. An applied migration is never
-- re-edited; it is corrected by the next one. This is that one.
--
-- NOTHING ELSE PINS THE VOCABULARY. `app.renders.responsive_profile` carries an
-- exact-pin CHECK (migration 154) and no enum, and
-- `app.render_shares.responsive_profile` is pinned to the literal 'share'
-- (migration 162) because a share page is a share page. Both stay as they are:
-- a Render pinned to `mcp-pip` was already storable, and only the build ledger
-- listed the vocabulary.

BEGIN;

ALTER TABLE app.renderer_runtime_builds
    DROP CONSTRAINT IF EXISTS ck_renderer_runtime_builds_profiles;
ALTER TABLE app.renderer_runtime_builds
    ADD CONSTRAINT ck_renderer_runtime_builds_profiles CHECK (
        cardinality(responsive_profiles) >= 1
        AND responsive_profiles <@ ARRAY[
            'console', 'mcp-inline', 'mcp-fullscreen', 'mcp-pip', 'share'
        ]::TEXT[]
    );

COMMENT ON COLUMN app.renderer_runtime_builds.responsive_profiles IS
    'The responsive profiles this renderer build supports. The vocabulary has ONE '
    'definition site -- server/core/visualization_specs.RESPONSIVE_PROFILES -- and '
    'this CHECK mirrors it. server/tests/core/test_visualization_specs.py reads '
    'the newest migration that defines this constraint and asserts the two lists '
    'are equal, so a value added to the tuple without its migration is a red test '
    'rather than a deploy-time refusal.';

COMMIT;
