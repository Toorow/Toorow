-- Epic 51: `mcp_app_behavior` still cannot pass -- for a reason that is now TRUE.
--
-- WHAT MIGRATION 166 GOT WRONG, two hours after writing it. It added
-- `ck_evaluation_case_verdicts_mcp_evaluator_absent` and stated, in the constraint's
-- own COMMENT, that "Story 50.6 (MCP data/render tool split) is ready-for-dev".
--
-- 50.6 was delivered at 12:10 the same day, commit `1e24348`. The sprint tracker
-- still said `ready-for-dev` when 166 was written, and I copied it instead of
-- reading the log. A constraint whose stated reason is false is worse than an
-- unexplained one: the next reader checks the reason, finds it settled, and lifts
-- the guard.
--
-- WHY THE CONSTRAINT NEVERTHELESS STAYS. Measured before deciding, and this is the
-- distinction 166 missed by reasoning from a story number rather than from what the
-- story produced:
--
--   * what 50.6 delivers is a CATALOG INVARIANT checked at BOOT --
--     `mcp_profiles.assert_data_render_split` runs inside `validate_catalog` on the
--     live registry and refuses to start the server, and
--     `enforce_result_model_channel` bounds every returning tool result. Both are
--     excellent, and neither produces a record of one execution;
--   * what this dimension judges is an INTERACTION (`analyze-and-test.md:326`):
--     "Render/Result parity, evidence drill-down, honest empty/stale/error/refusal
--     states, read-only View Tools, governed feedback and capability-safe host
--     fallback". That is per-execution behaviour, not a property of the catalog.
--
-- And nothing records it. Measured:
--
--     SELECT table_name FROM information_schema.tables
--      WHERE table_schema = 'app'
--        AND (table_name LIKE '%interaction%' OR table_name LIKE '%view_tool%'
--             OR table_name LIKE '%drill%');
--     -- zero rows
--
-- `app.renders` carries `display_state`, `evidence_manifest` and
-- `datum_evidence_keys`, which is the DECLARED presentation state of a Render. A
-- future evaluator can already judge Render/Result parity from them. What no table
-- holds is what a person actually did with it -- whether the drill-down resolved,
-- whether the host fallback was taken, which state was really shown.
--
-- So the blocker moved rather than disappeared: from "the rendered artifact does not
-- exist" (closed by 166) to "no observed interaction evidence exists". The guard is
-- the same; only its stated reason changes, and it is the reason a reader acts on.
--
-- This migration alters no structure. It replaces two comments, because a comment
-- that lies is a defect with a long fuse.

COMMENT ON CONSTRAINT ck_evaluation_case_verdicts_mcp_evaluator_absent
    ON app.evaluation_case_dimension_verdicts IS
    'A pass here would be a claim about nothing. Story 50.6 IS delivered (1e24348): '
    'it enforces the data/render tool split as a boot-time catalog invariant, which '
    'is not the same object as this dimension. What is judged is a per-execution '
    'interaction -- Render/Result parity, evidence drill-down, honest '
    'empty/stale/error/refusal states, read-only View Tools, governed feedback and '
    'host fallback (analyze-and-test.md:326) -- and no table records it: no '
    'interaction, view-tool or drill-down relation exists in `app`. Dropped by the '
    'story that emits that evidence, and by no other, because no other makes this '
    'false. The earlier comment said 50.6 was ready-for-dev; it was not, and copying '
    'a stale tracker line into a constraint is how a guard gets lifted for a reason '
    'that was never true.';

COMMENT ON COLUMN app.evaluation_case_dimension_verdicts.render_ref IS
    'The Render this verdict judged. Fillable since migration 166 (composite foreign '
    'key to app.renders), which is what the `mcp_needs_evidence` CHECK has always '
    'required for a pass. It is no longer the render pin that keeps '
    '`mcp_app_behavior` unreachable -- it is the absence of observed interaction '
    'evidence, stated on ck_evaluation_case_verdicts_mcp_evaluator_absent.';
