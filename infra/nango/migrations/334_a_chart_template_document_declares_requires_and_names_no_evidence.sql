-- 334: story 72.2 -- the Chart Template document has a grammar now, so the
-- columns say its name and the shape it must have.
--
-- WHY THIS MIGRATION EXISTS AT ALL. Migration 333 wrote, in the file, the one
-- thing it deliberately did not do:
--
--   "WHAT IS DELIBERATELY NOT PINNED HERE. `spec_contract_version` is bounded
--    and made to agree with the document, but its LITERAL is not fixed to a
--    value: story 72.2 owns the grammar and therefore owns the name of its
--    contract. Fixing a literal now would be this migration inventing a
--    decision, and the widening would have to be undone by another migration."
--
-- Story 72.2 has now named it. The literal is `chart-template.v1`, declared once
-- in `server/core/visualization_templates.py` as
-- `CHART_TEMPLATE_CONTRACT_VERSION`, and this file is where the database learns
-- it. `test_the_contract_literal_is_pinned_in_one_place` reads both and asserts
-- they are one string, so moving the constant without its migration is a red
-- test rather than a deploy-time surprise.
--
-- WHY THE OTHER THREE CHECKS, and why they are ADDITIVE. 333's
-- `ck_visualization_template_versions_is_unbound` is untouched: it refuses
-- `bindings`, `query_spec_version_id` and `result_id`, and it was right. What it
-- could not know, because the grammar was not written yet, is the rest of the
-- same ratified sentence -- `visualization-and-rendering.md`, § *Amendment,
-- 2026-08-31*: "a Chart Template carries a `member_id`, a
-- `query_spec_version_id`, a `result_id` OR A DATA VALUE: that is no longer a
-- starting point, it is a Visualization".
--
--   * `annotations` is a data value by that sentence. Every annotation of a
--     Visualization Spec names an `evidence_id` -- evidence that ONE Result
--     carries (`visualization_specs.py`, the `annotations` block). The template
--     grammar therefore subtracts the key whole, because an `anchor` with no
--     evidence to anchor says where to draw a note that names nothing. The
--     service refuses it by name with its JSON pointer; this CHECK is the layer
--     that holds when the service is bypassed, exactly as `..._is_unbound` is.
--   * `requires` is the positive half of the same statement, and it had no
--     layer at all. 333 could say what a template must NOT carry; only now can
--     it be said what a template IS -- a block of compatibility predicates,
--     `{well: {min, max, accepts, max_cardinality}}`. A row with no `requires`
--     is a presentation that requires nothing of a Result, which is not a
--     starting point.
--   * `answers_question` is the row of the list screen. The ratified list screen
--     "states the question the template answers" and the workbench Overview
--     shows "The question"; a template with none would make an empty cell that
--     no gesture fills. It is versioned with the document rather than held on
--     the head because changing what a template claims to answer changes what it
--     is, and that is an edit that must produce a new immutable version.
--
-- WHAT THIS MIGRATION DOES NOT DO. It does not touch
-- `ck_visualization_template_versions_family`: that CHECK already mirrors
-- `SPEC_SELECTABLE_FAMILY_IDS` (`server/core/visualization_families.py`) at its
-- eight families, and story 72.2 neither widens nor narrows the shipped
-- registry. It does not touch the size CHECK: `MAX_TEMPLATE_BYTES` is
-- `MAX_SPEC_BYTES`, so 333's `pg_column_size(document) <= 32768` is already the
-- mirror AC8 asks for, and a second number would be a second ceiling.
--
-- MEASURED BEFORE WRITING. Migration 333 is not applied in production (the
-- ledger head was 332 when it was written, and this file does not change that);
-- both tables are empty everywhere they exist, so every constraint below
-- validates against zero rows. No constraint here is dropped, weakened or
-- re-edited -- each one is added beside what 333 already said.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. The contract has a name.
-- ---------------------------------------------------------------------------
ALTER TABLE app.visualization_template_versions
    DROP CONSTRAINT IF EXISTS ck_visualization_template_versions_contract_pinned;
ALTER TABLE app.visualization_template_versions
    ADD CONSTRAINT ck_visualization_template_versions_contract_pinned
    CHECK (spec_contract_version = 'chart-template.v1');

COMMENT ON COLUMN app.visualization_template_versions.spec_contract_version IS
    'chart-template.v1 -- the grammar of story 72.2, derived from '
    'visualization-spec.v1 by subtracting its member anchors and adding the '
    'requires block. It travels inside the hashed document, so a content_hash '
    'is only comparable to one produced under the same contract. Declared once '
    'in server/core/visualization_templates.py.';

-- ---------------------------------------------------------------------------
-- 2. What a template IS: a block of compatibility predicates and a question.
-- ---------------------------------------------------------------------------
ALTER TABLE app.visualization_template_versions
    DROP CONSTRAINT IF EXISTS ck_visualization_template_versions_declares_requires;
ALTER TABLE app.visualization_template_versions
    ADD CONSTRAINT ck_visualization_template_versions_declares_requires
    CHECK (
        -- `?` FIRST, and it is not decoration. `document -> 'requires'` on an
        -- absent key is SQL NULL, `jsonb_typeof(NULL)` is NULL, and `NULL =
        -- 'object'` is NULL -- which a CHECK reads as satisfied. A constraint
        -- written without this test would accept the exact row it exists to
        -- refuse: a template that declares no predicate at all.
        document ? 'requires'
        AND jsonb_typeof(document -> 'requires') = 'object'
        AND document ? 'answers_question'
        AND jsonb_typeof(document -> 'answers_question') = 'string'
    );

-- ---------------------------------------------------------------------------
-- 3. What a template is still not: a name for evidence one Result carries.
-- ---------------------------------------------------------------------------
ALTER TABLE app.visualization_template_versions
    DROP CONSTRAINT IF EXISTS ck_visualization_template_versions_names_no_evidence;
ALTER TABLE app.visualization_template_versions
    ADD CONSTRAINT ck_visualization_template_versions_names_no_evidence
    CHECK (NOT (document ? 'annotations'));

COMMENT ON COLUMN app.visualization_template_versions.document IS
    'One chart-template.v1 document: the Visualization Spec grammar with every '
    'member anchor re-anchored on a well, its evidence anchors subtracted, and '
    'a requires block in place of bindings. Its walker is imported, never '
    'copied: server/core/visualization_templates.py derives the grammar from '
    'core.visualization_specs.GRAMMAR and validates with the same walker, so a '
    'change to the Spec grammar propagates or reddens a derivation guard.';

COMMIT;
