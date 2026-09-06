-- A change preparation records WHETHER its base was the version in force, or the
-- head of the ledger with nothing in force at all.
--
-- Migration 138 gave `app.datastream_change_preparations` two NOT NULL columns,
-- `expected_plan_version_id` and `expected_mapping_version_id`, and
-- `datastream_change.prepare_change` filled them from
-- `app.datastreams.current_plan_version_id` / `current_mapping_version_id`. Those
-- pointers are the optimistic lock: `confirm_change` refuses when they moved
-- between the review and the confirmation, which is what makes a concurrent
-- publication safe.
--
-- The Mapping tab became editable on the HEAD version when no pointer is in
-- force (amendment 4 of the 2026-08-11 review,
-- `docs/product-architecture/datastream-workbench-and-wizard.md`). Measured on
-- the live base on 2026-08-12: of 8 non-archived Datastreams, 1 is `active` with
-- both pointers and 7 are `draft`, 6 of which carry NO mapping pointer. So the
-- screen offers a governed change on the majority of the fleet and the seam
-- refuses every one of them.
--
-- Widening the seam needs ONE fact this table cannot state today: an expected
-- version id says WHICH version the review was frozen against, never whether
-- that version was in force. Both bases are a real version id -- the pointer's,
-- or the highest `version_number` recorded -- so neither column becomes nullable
-- and nothing about them is relaxed. What is added is the distinction, because
-- the two demand DIFFERENT checks at confirmation time:
--
--   in force     -- the pointer must still be that exact version. Unchanged.
--   head, none   -- no pointer may have appeared (a publication between the
--                   review and the confirmation makes a version live, and the
--                   base was reviewed as "nothing is live"), AND the head must
--                   still be that exact version (a concurrent append supersedes
--                   the document the person read).
--
-- DEFAULT TRUE, so every preparation written before this migration keeps exactly
-- the meaning it was written with: its base WAS a pointer in force, and its
-- confirmation is checked the way it always was.
--
-- This column adds no way to make a version live. The mapping pointer still
-- moves through governed publication only (`docs/product-architecture/
-- governance.md`, "The head is a POINTER, not a status"), whose single writer
-- stays `datastream_activation.publish_activate_mutation`.

ALTER TABLE app.datastream_change_preparations
    ADD COLUMN IF NOT EXISTS expected_plan_pointer_in_force BOOLEAN NOT NULL DEFAULT TRUE;

ALTER TABLE app.datastream_change_preparations
    ADD COLUMN IF NOT EXISTS expected_mapping_pointer_in_force BOOLEAN NOT NULL DEFAULT TRUE;

COMMENT ON COLUMN app.datastream_change_preparations.expected_plan_pointer_in_force IS
    'TRUE when expected_plan_version_id was app.datastreams.current_plan_version_id at '
    'preparation time; FALSE when no plan version was in force and the base is the head '
    'of the plan ledger. Selects which optimistic-lock check confirm_change applies.';

COMMENT ON COLUMN app.datastream_change_preparations.expected_mapping_pointer_in_force IS
    'TRUE when expected_mapping_version_id was app.datastreams.current_mapping_version_id at '
    'preparation time; FALSE when no mapping version was in force and the base is the head '
    'of the mapping ledger. Selects which optimistic-lock check confirm_change applies.';
