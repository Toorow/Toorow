-- 328 -- An annotation's earlier wording survives its correction, and a withdrawn
--        one is frozen whole.
--
-- WHAT AN ANNOTATION IS, decided by Jean on 2026-08-31: the manual context EVENT
-- and its DESCRIPTION. Story 49.6's AC3 asks that "previous" and "superseded" be
-- DISTINCT states rather than one word for "gone", and AC10 that a lifecycle
-- "never rewrites history". Migration 286 delivered the withdrawal half of that
-- sentence -- a retirement is a supersede, it names its author and its reason,
-- and it is never unmade. This migration delivers the two halves 286 left open.
--
-- ────────────────────────────────────────────────────────────────────────────
-- HALF ONE. A CORRECTION IS THE ONE WRITE ON THIS TABLE THAT STILL DESTROYS.
--
-- `core/context_events.py#update_manual_event` UPDATEs the row in place. The
-- superseded wording survives ONLY as a JSON blob inside `app.audit_log`
-- ("previous": {...}), which no reader can reach: `context_events_api.py`
-- answers every listed event with `"version_history": false` -- the API states
-- the absence in a literal boolean. So a narration published last week could
-- cite "Price change +12%" while today's row reads "Price change", and nothing
-- in the product can tell a reader that the annotation was re-worded, when, or
-- by whom. That is exactly the difference 286 refused to lose between "this was
-- never said" and "this was said, and withdrawn" -- one write to the left.
--
-- THE SUPERSEDED WORDING BECOMES A ROW. One row per correction, holding the
-- annotation AS IT READ BEFORE that correction, plus who corrected it, when, and
-- WHICH fields moved. Appended, never rewritten: a record of a wording that was
-- superseded is worth nothing if a later hand can edit it, which is the same
-- argument `app.context_relationship_versions` (317) and `app.daily_insights`
-- (321/324) already carry.
--
-- WHY THE PREVIOUS VALUES ARE COLUMNS AND NOT A JSONB. Because the audit blob is
-- the state this migration exists to leave: a payload nobody projects is a
-- payload nobody reads. A screen that must say "this said X until 2026-08-20"
-- reads `previous_label` and `previous_description`; it does not dig a key out of
-- an untyped document, and a column that stops being written is a column the
-- catalog shows.
--
-- WHY `corrected_fields` AS WELL, when a reader could diff two revisions. Two
-- revisions do not tell WHICH field the author meant to move: correcting only the
-- date leaves the label identical in both rows, and a diff computed downstream
-- would present the unchanged label as part of the correction. The writer knows;
-- it says so once here.
--
-- WHY NO INDEX BEYOND THE UNIQUE ONE. The only read is "the revisions of THIS
-- event, oldest first" -- `uq_context_event_revisions_no (event_id, revision_no)`
-- covers both the lookup and the order. A second index would cost every write and
-- repay no read, which is the decision migration 286 already stated for the
-- retirement columns.
--
-- WHAT AN ERASURE REACHES, said plainly rather than assumed -- and MEASURED,
-- because the first spelling of this comment claimed the opposite from memory:
-- `app.context_events` DOES carry `fk_context_events_project` (ON DELETE
-- RESTRICT), so the parent IS in the erasure plan and its rows are deleted
-- explicitly. For the table THIS migration creates, as migration 235:54-61
-- measured for its own tables, it is NOT `core.org_purge` that reaches it:
-- `plan_purge` walks `confdeltype IN ('a','r')` only, and a CASCADE edge is
-- deliberately absent from its plan -- the parent statement that cascades down
-- to `app.context_event_revisions` is the `DELETE FROM app.context_events`
-- the purge emits for the annotations themselves; Postgres does the child.
-- The reach is kept, not widened: the child cascades from
-- the parent (`ON DELETE CASCADE`), the append-only guard below carries no DELETE
-- bit -- so a DELETE reaches the row whatever the trigger thinks -- and DELETE is
-- granted at the privilege level, which is the half migration 198 measured being
-- forgotten. The day the parent joins the tenant tree, the revisions follow it
-- with no further work.
--
-- ────────────────────────────────────────────────────────────────────────────
-- HALF TWO. THE TWO GAPS MIGRATION 324 CLOSED ON `app.daily_insights` ARE OPEN
-- HERE, WORD FOR WORD.
--
-- 324's adversarial review found two holes in 321's retraction guard. 286's guard
-- has the same shape and therefore the same two holes:
--
--   (1) BORN RETIRED. 286 guards UPDATE only, so an INSERT already carrying
--       `retired_at`/`retired_by`/`retired_reason` files a withdrawal nobody
--       performed and nothing audited -- a forged withdrawal, at birth.
--       `persist_context_event` never names those columns, which makes this a
--       constraint instead of a habit.
--
--   (2) FROZEN ONLY IN THREE COLUMNS. 286 freezes the retirement triple and
--       nothing else, so at the SQL layer a withdrawn annotation can still be
--       re-worded: its label, its description, its date, the metric it claims to
--       be about. `_locked_manual_event` refuses that in Python, and a refusal
--       that lives only in the service is a refusal one other writer undoes. The
--       honest rule is the one 324 states: after the withdrawal is filed, the row
--       IS the record; nothing on it changes, ever.
--
-- 286 is APPLIED and is never re-edited (the checksum ledger forbids it); this
-- migration REPLACES its function and re-creates its trigger, which is the
-- ratified repair path -- fix by the next one.
--
-- ORDER OF FIRING, stated rather than discovered. Trigger names fire
-- alphabetically: `trg_context_events_freeze_binding` (212), then
-- `trg_context_events_freeze_retirement` (this one), then
-- `trg_context_events_require_binding` (133/287). None reads what another writes.
--
-- Contract: docs/product-architecture/context-hub.md.

BEGIN;

-- ── Half one: the superseded wording ────────────────────────────────────────

CREATE TABLE IF NOT EXISTS app.context_event_revisions (
    id                   TEXT        PRIMARY KEY,
    event_id             TEXT        NOT NULL
                                     REFERENCES app.context_events (id)
                                     ON DELETE CASCADE,
    -- Denormalised on purpose: every read of this table is scoped by project
    -- before it is scoped by event, and a scope that needs a join is a scope one
    -- reader eventually forgets.
    project_id           TEXT        NOT NULL,
    revision_no          INTEGER     NOT NULL,
    superseded_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    corrected_by         TEXT        NOT NULL,
    corrected_fields     TEXT[]      NOT NULL,
    -- The annotation as it read BEFORE this correction. The three the create door
    -- refuses blank are NOT NULL here for the same reason.
    previous_event_date  DATE        NOT NULL,
    previous_type        TEXT        NOT NULL,
    previous_label       TEXT        NOT NULL,
    previous_description TEXT,
    previous_platform    TEXT,
    previous_value       NUMERIC,
    previous_metric      TEXT,
    previous_entity_key  TEXT,
    previous_entity_kind TEXT,
    CONSTRAINT ck_context_event_revisions_fields
        CHECK (cardinality(corrected_fields) > 0),
    CONSTRAINT ck_context_event_revisions_author
        CHECK (btrim(corrected_by) <> ''),
    CONSTRAINT ck_context_event_revisions_no
        CHECK (revision_no >= 1),
    CONSTRAINT uq_context_event_revisions_no UNIQUE (event_id, revision_no)
);

COMMENT ON TABLE app.context_event_revisions IS
    'One row per correction of a manual context event, holding the annotation as '
    'it read BEFORE that correction. Append-only: a superseded wording a later '
    'hand can edit proves nothing. Story 49.6 AC3/AC10 -- "previous" and '
    '"superseded" are distinct states, and a lifecycle never rewrites history.';
COMMENT ON COLUMN app.context_event_revisions.corrected_fields IS
    'WHICH fields the author moved, in the writer''s own words. A diff computed '
    'downstream cannot tell a field that was corrected from one that happened to '
    'stay identical.';
COMMENT ON COLUMN app.context_event_revisions.revision_no IS
    'Position in this event''s history, 1 for the first wording superseded. '
    'Allocated under the FOR UPDATE lock the correction already holds.';

CREATE OR REPLACE FUNCTION app.freeze_context_event_revision()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'a context event revision is the record of a wording that was superseded '
        'on %: it is never rewritten. Correct the event again -- the correction '
        'files the next revision',
        OLD.superseded_at
        USING ERRCODE = '23514';
END;
$$;

-- UPDATE only. No DELETE bit, deliberately: the day `app.context_events` joins
-- the tenant tree, the erasure must pass here without a hatch to negotiate --
-- the same posture 321 and 324 chose, pinned by
-- `test_the_erasure_hatch_is_a_privilege_too.py`.
DROP TRIGGER IF EXISTS trg_context_event_revisions_append_only
    ON app.context_event_revisions;
CREATE TRIGGER trg_context_event_revisions_append_only
    BEFORE UPDATE ON app.context_event_revisions
    FOR EACH ROW EXECUTE FUNCTION app.freeze_context_event_revision();

-- Migration 207's `ALTER DEFAULT PRIVILEGES` hands SELECT, INSERT, UPDATE, DELETE
-- to `connector` on every FUTURE table, so a narrow GRANT here would be
-- declarative only -- it adds nothing and revokes nothing (migration 316's
-- finding). The REVOKE is written beside the GRANT, the day the table is created,
-- which is the only day the repair costs nothing (317's posture).
GRANT SELECT, INSERT, DELETE ON app.context_event_revisions TO connector;
REVOKE UPDATE ON app.context_event_revisions FROM connector;

-- ── Half two: 324's two closures, on the annotation ─────────────────────────

CREATE OR REPLACE FUNCTION app.freeze_context_event_retirement()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        -- The write that FILES a withdrawal is an UPDATE on a standing row,
        -- audited beside it. A row born retired is a withdrawal nobody performed.
        IF NEW.retired_at IS NOT NULL
           OR NEW.retired_by IS NOT NULL
           OR NEW.retired_reason IS NOT NULL THEN
            RAISE EXCEPTION
                'a context event is never born retired: write the annotation, then '
                'withdraw it through the retirement door so the withdrawal is audited'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.retired_at IS NOT NULL THEN
        -- 286's OWN SENTENCE FIRST, and it is kept word for word rather than
        -- generalised away. A caller trying to UN-RETIRE and a caller trying to
        -- re-word a withdrawn row need different gestures -- "write the event
        -- again instead of un-retiring this one" is useless advice to the
        -- second, and "instead of re-wording this one" is useless to the first.
        -- A guard that widened its scope and lost its precision would trade one
        -- defect for another.
        IF NEW.retired_at IS DISTINCT FROM OLD.retired_at
           OR NEW.retired_by IS DISTINCT FROM OLD.retired_by
           OR NEW.retired_reason IS DISTINCT FROM OLD.retired_reason THEN
            RAISE EXCEPTION
                'a context event retirement is never unmade: % was retired at % by %; '
                'write the event again instead of un-retiring this one',
                OLD.id, OLD.retired_at, OLD.retired_by
                USING ERRCODE = '23514';
        END IF;

        -- After the withdrawal is filed, the whole row is the record of it: the
        -- day it named, the wording a narration cited, the metric it claimed to
        -- be about. Nothing on it changes, ever. (`NEW IS DISTINCT FROM OLD`
        -- compares every column, so a column added later is frozen with the rest
        -- by construction.)
        IF NEW IS DISTINCT FROM OLD THEN
            RAISE EXCEPTION
                'context event % was retired at % by %: a withdrawn annotation is '
                'frozen whole. Write the event again instead of re-wording this one',
                OLD.id, OLD.retired_at, OLD.retired_by
                USING ERRCODE = '23514';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_context_events_freeze_retirement ON app.context_events;
CREATE TRIGGER trg_context_events_freeze_retirement
    BEFORE INSERT OR UPDATE ON app.context_events
    FOR EACH ROW EXECUTE FUNCTION app.freeze_context_event_retirement();

COMMIT;
