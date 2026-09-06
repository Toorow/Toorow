-- 286 -- A manual context event can be corrected, and retired without being erased.
--
-- THE MEASUREMENT. reviews/audit-2026-08-17/08-context-hub.md, P1 item 2: the
-- only writers of `app.context_events` are `add_context_event` (MCP) and
-- `POST /api/context-events`. There is no update path and no human removal path.
-- `context_events.py:235 delete_connector_events_in_window` is the sole DELETE,
-- and it is scoped so it can never touch `source = 'manual'`. So a manual event
-- carrying the wrong date or the wrong label feeds `core/narrative.py`,
-- `core/summarizer.py`, `get_events` and the "Why" of every narration FOREVER.
-- The class the audit states: any table the product cites as a CAUSE must carry
-- a human correction path.
--
-- ONLY THE MANUAL HALF GETS ONE, and that is not a limitation of this migration.
-- A row whose `source` is a Connector is DERIVED: its owner is that Datastream's
-- Event Configuration, and the next pull re-emits it (migration 133 / 212 bind
-- it to the version that produced it). Correcting such a row by hand writes a
-- value the next ingest erases -- a repair that lies. The columns below exist for
-- every row because a column cannot be conditional; the REFUSAL lives in
-- `core/context_events.py`, and it names the gesture (edit the Event
-- Configuration) rather than the cause.
--
-- WHY RETIREMENT IS A SUPERSEDE AND NOT A DELETE. An event that has been read is
-- evidence. It was a candidate cause in an anomaly alert, a marker on a card, a
-- regressor in an MMM feature matrix, a line in a briefing -- all of them
-- computed and, several of them, PERSISTED. Deleting the row makes those earlier
-- answers unexplainable: the product loses the ability to tell "this was never
-- said" from "this was said, and later withdrawn", which is the same third state
-- migration 262 refused to create for `entity_key`. The row therefore stays, and
-- stops being SERVED -- the reads add `retired_at IS NULL`.
--
-- The three columns move together or not at all. A retirement with no author is
-- an anonymous erasure, and one with no reason is a row nobody can ever judge:
-- the next reader has to decide whether the event was wrong or merely
-- inconvenient, and nothing on the row answers. Same pairing style, and same
-- argument, as `ck_context_events_entity_pair` (migration 262).
--
-- WHY A RETIREMENT IS IMMUTABLE. Un-retiring makes one row alternately a cause
-- and a non-cause, and every derived answer silently changes meaning with no
-- trace of which state produced it -- exactly the lie migration 212 closed for
-- `binding_state`. A retirement filed by mistake is repaired the way this table
-- repairs everything else: by writing the event again. That is what the reason
-- column is for.
--
-- WHY A SECOND TRIGGER FUNCTION, AND NOT 212's. `app.freeze_event_observation_
-- binding` protects the BINDING; this protects the RETIREMENT. Folding both into
-- one function would tie two invariants to one body, so a future correction to
-- either silently re-opens the other -- which is the argument 212 itself gives
-- for not reusing the 133 function. A migration is never re-edited (CLAUDE.md);
-- the correction comes in the next one.
--
-- NO NEW INDEX, and that is a decision rather than an omission. The default read
-- is `_list_context_events` (`context_events_api.py:253-268`):
-- `WHERE project_id = ? [AND event_date >= ?] [AND event_date <= ?]
--  ORDER BY event_date DESC, created_at DESC`. Migration 009 already poses
-- `ctx_events_project_date (project_id, event_date)`, which covers that WHERE and
-- the leading ORDER BY term. `retired_at IS NULL` narrows a set already bounded
-- to one project's date window, and retirement is an EXCEPTION rather than a
-- lifecycle stage -- migration 262 measured 3 rows on the reference project. A
-- partial copy of an existing index, to discard near-zero rows, is an index that
-- costs every write and repays no read.
--
-- Contract: docs/product-architecture/context-hub.md.

BEGIN;

ALTER TABLE app.context_events
    ADD COLUMN IF NOT EXISTS retired_at     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS retired_by     TEXT,
    ADD COLUMN IF NOT EXISTS retired_reason TEXT;

-- All three, or none. A blank author or a blank reason is the same absence
-- wearing a value, so it is refused here rather than trusted from the writer.
ALTER TABLE app.context_events
    DROP CONSTRAINT IF EXISTS ck_context_events_retirement_triple;
ALTER TABLE app.context_events
    ADD CONSTRAINT ck_context_events_retirement_triple CHECK (
        (retired_at IS NULL AND retired_by IS NULL AND retired_reason IS NULL)
     OR (retired_at IS NOT NULL
         AND retired_by IS NOT NULL AND btrim(retired_by) <> ''
         AND retired_reason IS NOT NULL AND btrim(retired_reason) <> '')
    );

CREATE OR REPLACE FUNCTION app.freeze_context_event_retirement()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    -- A retirement is never unmade, and never re-attributed. Before it is filed
    -- the three columns are freely writable -- that is the retirement itself.
    -- After, they are the record of a withdrawal that already happened.
    IF OLD.retired_at IS NOT NULL THEN
        IF NEW.retired_at IS DISTINCT FROM OLD.retired_at
           OR NEW.retired_by IS DISTINCT FROM OLD.retired_by
           OR NEW.retired_reason IS DISTINCT FROM OLD.retired_reason THEN
            RAISE EXCEPTION
                'a context event retirement is never unmade: % was retired at % by %; '
                'write the event again instead of un-retiring this one',
                OLD.id, OLD.retired_at, OLD.retired_by
                USING ERRCODE = '23514';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

-- Its own trigger, alongside 212's, for the reason stated in the header. The
-- name sorts after `..._freeze_binding`, so the binding guard runs first and
-- this one sees the NEW row it returned -- neither reads what the other writes,
-- but the order is stated rather than discovered.
DROP TRIGGER IF EXISTS trg_context_events_freeze_retirement ON app.context_events;
CREATE TRIGGER trg_context_events_freeze_retirement
    BEFORE UPDATE ON app.context_events
    FOR EACH ROW EXECUTE FUNCTION app.freeze_context_event_retirement();

COMMENT ON COLUMN app.context_events.retired_at IS
    'When this event stopped being served as live. NULL is the only "live" state; '
    'a retired row is kept for audit and excluded from every read that claims to '
    'list what a project observes. A retirement is never unmade (trigger).';
COMMENT ON COLUMN app.context_events.retired_by IS
    'Who retired it -- the caller identity, never a token (AD-3). An anonymous '
    'withdrawal is indistinguishable from an erasure.';
COMMENT ON COLUMN app.context_events.retired_reason IS
    'Why it stopped being read, in the author''s words. Without it a later reader '
    'cannot tell a wrong event from an inconvenient one.';

COMMIT;
