-- The `mcp_app` writer was never blocked by the effect vocabulary.
--
-- Migration 175 shipped `app.evidence_inspections` with
-- `surface IN ('console', 'mcp_app', 'share')` and recorded, on
-- `ck_evaluation_case_verdicts_mcp_evaluator_absent`, that the `mcp_app` value
-- had no writer for a stated reason:
--
--     "an app-visibility MCP tool cannot be declared under the three effects of
--      AD-24 without lying about one of them, so that slot is a decision at
--      core/mcp_profiles.py"
--
-- and it named a fourth effect, or an app-only exemption, as the way out.
--
-- MEASURED BEFORE WRITING THIS. Neither is needed; the exemption already exists.
--
--     grep -n "_record_app_declaration" -A 20 server/core/mcp_profiles.py
--       -> an app-only tool must be effect="read" / confirmation_mode="none",
--          enforced AT REGISTRATION, since Story 50.6
--     grep -rn 'visibility=\["app"\]' server/core/*.py
--       -> analyze_render_mcp.py:579, :628 -- two tools already ship under it
--
-- So the slot is `register_profiled(..., app=AppConfig(visibility=["app"]))`
-- with `effect="read"`, and it was declarable the whole time.
--
-- WHY `read` IS NOT THE LIE 175 THOUGHT IT WAS. AD-24's `effect` classifies what
-- a tool does to DOMAIN state -- "a read cannot mutate, a prepare cannot
-- authorize, and a confirmed write must use AD-27". It does not classify the
-- audit a call leaves behind, and it cannot: AD-28 REQUIRES "every read of
-- sensitive samples, account exposure, ..." to commit "the state transition,
-- append-only audit event and outbox record together". A vocabulary in which
-- writing an append-only audit row promoted a tool out of `read` would make
-- AD-28 unimplementable for every read tool in the catalog.
--
-- An inspection row is exactly that class, and 175 said so itself: append-only,
-- no domain transition, reachable by the audited RGPD erasure, "evidence that a
-- surface was USED, never a second copy of what it displayed".
--
-- The decision is executable rather than merely written down:
-- `server/tests/conformance/test_app_only_observation_writer.py` registers an
-- observation writer under the app-only slot, passes `validate_catalog`, and
-- proves that `confirmed_write` and `prepare` are both REFUSED at the
-- registration site -- which is what makes `read` the only declarable answer
-- rather than the convenient one.
--
-- WHAT REMAINS TRUE ON THAT CONSTRAINT, AND IS NOT WEAKENED HERE.
--   a. No EVALUATOR reads `app.evidence_inspections`. `core/evaluation_runs.py`
--      still writes `unverifiable` for `mcp_app_behavior` unconditionally. The
--      guard stays, and only the story that delivers the evaluator drops it.
--   b. The `mcp_app` surface still has no writer REGISTERED. That is now a piece
--      of ordinary work -- one app-only tool calling
--      `core/evidence_inspections.insert_inspection` with `surface='mcp_app'` --
--      and no longer a vocabulary question anybody has to reopen.
--   c. `submit_feedback` is still registered on plain `mcp.tool` in
--      `core/main.py`, so it carries NO profile/effect declaration at all. That
--      is a hole in the CATALOG, not in the vocabulary, and it belongs to
--      whoever holds `main.py`.
--
-- Only comments change. No table, no column, no constraint, no row: the
-- constraint keeps its exact semantics, and what is replaced is a sentence that
-- has stopped being true. Migration 175 did the same to migration 168's
-- sentence, for the same reason it gave there -- "a comment that lies is a
-- defect with a long fuse".
--
-- Schema-Change-Checklist (CONTRIBUTING.md):
--   [x] Additive & idempotent (COMMENT ON is replace-in-place; replayable)
--   [x] No new column
--   [x] No destructive DROP/ALTER on populated columns
-- ============================================================================

BEGIN;

COMMENT ON CONSTRAINT ck_evaluation_case_verdicts_mcp_evaluator_absent
    ON app.evaluation_case_dimension_verdicts IS
    'A pass here would still be a claim about nothing, but for ONE remaining '
    'reason, not the two migration 175 recorded. (1) No EVALUATOR reads the '
    'evidence: core/evaluation_runs.py still writes `unverifiable` for this '
    'dimension unconditionally, and nothing forms a verdict from an inspection. '
    'That is what this guard is now about, and only the story delivering the '
    'evaluator (epic 51) drops it. (2) 175 also said the mcp_app surface could '
    'have no writer because an app-visibility MCP tool "cannot be declared under '
    'the three effects of AD-24 without lying". That is NOT true and was not true '
    'when it was written: mcp_profiles._record_app_declaration has required '
    'app-only tools to be effect=read/confirmation_mode=none since Story 50.6, '
    'and two tools already ship under it. `read` is honest because AD-24 effects '
    'classify DOMAIN state while AD-28 separately requires every read to leave an '
    'append-only audit row -- an observation is that row, not a mutation. No '
    'fourth effect, no new exemption. Registering the mcp_app writer is ordinary '
    'work; conformance test test_app_only_observation_writer.py proves the slot '
    'accepts it and refuses the two declarations that would have lied.';

COMMENT ON COLUMN app.evidence_inspections.displayed_state IS
    'The state the person was actually shown. `branches_not_recorded` was the '
    'permanent answer for a persisted walk until migration 176 gave '
    'app.ai_path_steps a `detail` column and the recorder began keeping the '
    'candidates Story 54.2 judges -- watched or not. It stays in the vocabulary, '
    'and it is still NOT a zero: a step recorded before 176, or one whose '
    'crossings could not be written, has nothing to list, and collapsing that '
    'into `no_branch_judged` would make "we did not keep this" read as "nothing '
    'was considered".';

COMMIT;
