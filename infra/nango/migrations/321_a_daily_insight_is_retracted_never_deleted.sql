-- 321 -- A published daily insight is retracted by an audited transition, never deleted.
--
-- THE MEASUREMENT. `docs/product-architecture/proactive-assertions.md`, decision 4
-- and its `Incomplete if` clause: "a retraction is delivered as a delete rather
-- than an audited state transition". Measured at HEAD on 2026-08-30:
-- `grep -rn 'retract\|retraction' server ui infra` (excluding "contract") returned
-- two hits, neither of them on this family. NOTHING existed on any of the three
-- proactive stores -- `app.daily_insights` (061 / 281), `app.morning_briefings`
-- (017), `app.alert_firings` (011). The only way to unsay a published insight was
-- a DELETE nobody had written, which is the defect the decision names.
--
-- THE PATTERN IS MIGRATION 286's, ONE SURFACE OVER, and it is copied rather than
-- re-invented: a manual context event is retired, not erased, on three columns
-- that move together, with a trigger that refuses to unmake the retirement. The
-- argument transfers verbatim, and its premise is stronger here. A published
-- insight IS a proactive assertion: it was volunteered, it arrived with the
-- authority of having been selected as worth saying, and it may already have been
-- read, quoted, forwarded, or opened at the Result its publication produced
-- (migration 281). Deleting the row destroys the evidence that the claim was ever
-- made, and the product then cannot tell "this was never said" from "this was
-- said, and later withdrawn" -- the same third state migration 262 refused to
-- create for `entity_key`, and the opposite defect from the one being repaired.
--
-- WHY THE ITEM AND NOT THE RUN. `app.daily_insight_runs` records what the
-- operator's host DID on a date; `app.daily_insights` records what was CLAIMED.
-- A withdrawal is about the claim: the run still happened, its status is still
-- `published`, and rewriting the run to say otherwise would be toorow reporting a
-- day differently from the way it observed it -- what `execution-substrate.md`'s
-- host-scheduled clause forbids in its own words. So the columns sit on the item.
--
-- WHY THREE COLUMNS AND NOT ONE. A withdrawal with no author is an anonymous
-- erasure wearing a timestamp; one with no reason is a row nobody can ever judge,
-- because the next reader has to decide whether the claim was wrong or merely
-- inconvenient and nothing on the row answers. Same pairing rule, same CHECK
-- shape, as `ck_context_events_retirement_triple` (286).
--
-- WHY A RETRACTION IS IMMUTABLE. Un-retracting makes one insight alternately a
-- claim and a non-claim, and every reader that already excluded it silently
-- changes meaning with no trace of which state it read. A retraction filed by
-- mistake is repaired the way this family repairs everything: by publishing
-- again. Publication is idempotent per `(project_id, insight_date, slot)`, so the
-- write path REFUSES to overwrite a retracted slot and names the free one --
-- otherwise the republished claim would be born retracted, which is the same lie
-- read from the other end (`core/daily_insights.py:record_run`).
--
-- THE RGPD ERASURE HATCH IS UNTOUCHED, and that is a decision rather than luck.
-- This migration adds NO delete guard. `app.daily_insights` cascades from
-- `app.daily_insight_runs`, which cascades from `app.projects` (061), so an org
-- erasure still reaches these rows by the path it always used; the guard added
-- here is `BEFORE UPDATE` and a DELETE never fires it. Adding an append-only
-- DELETE trigger would have put the table inside the scope of
-- `server/tests/conformance/test_the_erasure_hatch_is_a_privilege_too.py` and made
-- the erasure depend on a DELETE privilege 061 never granted -- a right-to-erasure
-- request refused with 42501 before any trigger is reached. The retraction is an
-- editorial state; erasure is a different, audited operation, and they do not
-- share a mechanism.
--
-- NO NEW INDEX, for migration 286's reason and on this table's own numbers. The
-- reads are `WHERE project_id = %s AND insight_date = %s` and
-- `WHERE id = %s AND project_id = %s`, both already covered by
-- `daily_insights_project_date` (061) and the primary key, and both bounded to at
-- most three rows by `uq_daily_insights_project_date_slot`. A partial index to
-- discard a retracted row out of three is an index that costs every write and
-- repays no read.
--
-- Contract: docs/product-architecture/proactive-assertions.md (decision 4).
--
-- STRICTLY ADDITIVE. IDEMPOTENT.

BEGIN;

ALTER TABLE app.daily_insights
    ADD COLUMN IF NOT EXISTS retracted_at     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS retracted_by     TEXT,
    ADD COLUMN IF NOT EXISTS retracted_reason TEXT;

-- All three, or none. A blank author or a blank reason is the same absence
-- wearing a value, so it is refused here rather than trusted from the writer.
ALTER TABLE app.daily_insights
    DROP CONSTRAINT IF EXISTS ck_daily_insights_retraction_triple;
ALTER TABLE app.daily_insights
    ADD CONSTRAINT ck_daily_insights_retraction_triple CHECK (
        (retracted_at IS NULL AND retracted_by IS NULL AND retracted_reason IS NULL)
     OR (retracted_at IS NOT NULL
         AND retracted_by IS NOT NULL AND btrim(retracted_by) <> ''
         AND retracted_reason IS NOT NULL AND btrim(retracted_reason) <> '')
    );

CREATE OR REPLACE FUNCTION app.freeze_daily_insight_retraction()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    -- Before the retraction is filed the three columns are freely writable --
    -- that write IS the retraction. After, they are the record of a withdrawal
    -- that already happened, and nothing rewrites them.
    IF OLD.retracted_at IS NOT NULL THEN
        IF NEW.retracted_at IS DISTINCT FROM OLD.retracted_at
           OR NEW.retracted_by IS DISTINCT FROM OLD.retracted_by
           OR NEW.retracted_reason IS DISTINCT FROM OLD.retracted_reason THEN
            RAISE EXCEPTION
                'a daily insight retraction is never unmade: % was retracted at % by %; '
                'publish the day again on a free slot instead of un-retracting this one',
                OLD.id, OLD.retracted_at, OLD.retracted_by
                USING ERRCODE = '23514';
        END IF;

        -- And the CLAIM under a filed retraction is frozen with it. Publication
        -- upserts `payload` by slot, so without this line a republication would
        -- swap the prose beneath a withdrawal that names a reason for the OLD
        -- prose -- a retraction still standing over a claim it never judged.
        -- The write path refuses this case by name; this is the guard that makes
        -- the refusal an invariant rather than a politeness.
        IF NEW.payload IS DISTINCT FROM OLD.payload
           OR NEW.payload_hash IS DISTINCT FROM OLD.payload_hash THEN
            RAISE EXCEPTION
                'insight % was retracted at %: its claim is frozen with the withdrawal. '
                'Publish the new reading on a free slot of the same day',
                OLD.id, OLD.retracted_at
                USING ERRCODE = '23514';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_daily_insights_freeze_retraction ON app.daily_insights;
CREATE TRIGGER trg_daily_insights_freeze_retraction
    BEFORE UPDATE ON app.daily_insights
    FOR EACH ROW EXECUTE FUNCTION app.freeze_daily_insight_retraction();

COMMENT ON COLUMN app.daily_insights.retracted_at IS
    'When this published claim stopped being asserted. NULL is the only "standing" '
    'state; a retracted row is KEPT -- it is the evidence that the claim was made -- '
    'and is shown as withdrawn wherever the day is read, while every surface that '
    'volunteers assertions stops carrying it. A retraction is never unmade (trigger).';
COMMENT ON COLUMN app.daily_insights.retracted_by IS
    'Who retracted it -- the caller identity, never a token (AD-3). An anonymous '
    'withdrawal is indistinguishable from an erasure.';
COMMENT ON COLUMN app.daily_insights.retracted_reason IS
    'Why the claim was withdrawn, in the author''s words. Without it a later reader '
    'cannot tell a wrong insight from an inconvenient one.';

COMMIT;
