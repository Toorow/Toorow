-- 284: the wizard draft state `exited` is retired -- it was never reachable.
--
-- MEASURED BEFORE DECIDING (audit 2026-08-17, report 05, "l'etat exited sans
-- ecrivain"). Repo-wide census of the literal:
--
--   WRITERS: zero. Not one `UPDATE ... SET state='exited'`, no INSERT, no Python
--   constant, no API field. The only assignment anywhere in the repository is a
--   hand-written object in a vitest fixture
--   (`ui/admin/src/__tests__/DataWorkspacePage.test.tsx`).
--
--   READERS: five, and EVERY ONE of them spells it `draft OR exited` --
--   migration 134's partial index below, `datastream_preconfiguration.py` (the
--   resume lookup), `datastream_activation.py` twice (the materialization
--   guard), and `DataWorkspace.tsx`. Because the second disjunct is always
--   false, removing the state changes no behaviour at all.
--
--   RATIFIED TARGET: `docs/product-architecture/` never mentions it. The word
--   "exit" in `datastream-workbench-and-wizard.md` means LEAVE THE SCREEN, not
--   retire the object, and the `Save and exit` button honours that -- it PATCHes
--   through `update_draft`, which writes `state='draft'`. The one gesture named
--   "exit" writes the opposite state.
--
-- WHY IT IS NOT REUSED FOR "ABANDONED", which was the tempting repair. Both the
-- partial unique index below and the resume query count `exited` as LIVE: it
-- occupies the Project's single resumable-draft slot, and activation accepts it
-- as a legal predecessor of `materialized`. Marking a draft abandoned that way
-- would leave it resumable and materializable -- a semantic inversion, not a
-- lifecycle. An abandon path needs a state OUTSIDE the resumable predicate.
--
-- WHY `archived` STAYS, though it is unwritten too. It is exactly that state:
-- declared in 134, outside the resumable predicate, and correctly shaped for the
-- terminal path this product does not yet have. Report 05's open question 3 --
-- explicit abandon, or expiry, and after how long -- is an arbitration for Jean,
-- not a defect to guess at here. Retiring the state that cannot express the
-- answer while keeping the one that can is the whole of this migration.
--
-- No row is lost: the normalisation below is behaviour-identical, because every
-- reader already treated the two words as one.

BEGIN;

-- Behaviour-identical by construction (see the census above). Measured on the
-- disposable base at write time: zero rows carried the state anywhere.
UPDATE app.datastream_setup_drafts
   SET state = 'draft'
 WHERE state = 'exited';

ALTER TABLE app.datastream_setup_drafts
    DROP CONSTRAINT datastream_setup_drafts_state_check;

ALTER TABLE app.datastream_setup_drafts
    ADD CONSTRAINT datastream_setup_drafts_state_check
    CHECK (state IN ('draft', 'materialized', 'archived'));

-- The resumable set loses a member that was never in it. `archived` stays out,
-- which is what makes it usable as the terminal state later.
DROP INDEX IF EXISTS app.uq_datastream_setup_draft_resumable;
CREATE UNIQUE INDEX uq_datastream_setup_draft_resumable
    ON app.datastream_setup_drafts(project_id) WHERE state = 'draft';

COMMENT ON COLUMN app.datastream_setup_drafts.state IS
    'draft = the one resumable draft of the Project; materialized = it became a '
    'Datastream; archived = declared and deliberately unwritten, reserved for the '
    'terminal path (report 05 open question 3). The state `exited` was retired in '
    'migration 284: it had no writer, and every reader spelled it `draft OR exited`.';

COMMIT;
