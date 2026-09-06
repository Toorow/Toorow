-- 332: the draft state `archived` has a writer -- `Discard this draft`.
--
-- RATIFIED BY JEAN, 2026-08-31 (AI-336), and written in
-- `docs/product-architecture/datastream-workbench-and-wizard.md`, amendment
-- "a wizard draft ends by being discarded", in the same commit as this file.
--
-- WHAT THIS MIGRATION CHANGES: one COMMENT, and nothing else. No column, no
-- constraint, no index -- the state was already declared by migration 134 and
-- deliberately kept by 284, OUTSIDE the resumable predicate, precisely so a
-- terminal path could land on it without a schema change. What was missing was
-- never the shape, it was the writer, and the writer is Python:
-- `datastream_preconfiguration.discard_draft`, reached by
-- `DELETE /api/projects/{project_id}/datastream-setup-drafts/{draft_id}`.
--
-- WHY A MIGRATION AT ALL, then. Because 284's comment says the opposite of what
-- is now true -- "archived = declared and deliberately unwritten" -- and it says
-- it in the database, where the next person reads the column before they read
-- the code. A comment that describes a decision which has since been reversed is
-- the same defect as a screen stating a server fact that stopped being true; the
-- correction goes in the NEXT migration, never by editing 284.
--
-- WHAT STAYS EXACTLY AS IT WAS, and must:
--
--   * `uq_datastream_setup_draft_resumable` still covers `state = 'draft'` only.
--     A discarded draft leaves the Project's single resumable slot the instant
--     it is discarded -- that is the whole reason 284 refused to reuse `exited`,
--     whose predicate counted it as live.
--   * `CHECK ((state = 'materialized') = (materialized_datastream_id IS NOT
--     NULL))` is untouched, and it is what makes "a materialized draft cannot be
--     discarded" a database fact and not only a Python one.
--
-- NO EXPIRY, and that absence is the decision, not an omission: nothing here
-- archives a draft on age. Nobody measured a duration, and a job that retires
-- somebody's unfinished configuration while they are away is a deletion nobody
-- consented to. A sweep would need its own arbitration.

BEGIN;

COMMENT ON COLUMN app.datastream_setup_drafts.state IS
    'draft = the one resumable draft of the Project; materialized = it became a '
    'Datastream; archived = the person discarded it (AI-336, ratified 2026-08-31) '
    '-- a soft archive, written by `discard_draft`, outside the resumable index, '
    'and terminal: no restore, and every write refuses it. The state `exited` was '
    'retired in migration 284: it had no writer, and every reader spelled it '
    '`draft OR exited`.';

COMMIT;
