-- Story 54.2 / 55.2: the branches a walk judged survive the person watching it.
--
-- WHAT WAS MEASURED, BEFORE WRITING THIS.
--
--     \d app.ai_path_steps            -- no `detail` column (migration 150)
--     grep -n "def append_step" -A 15 server/core/ai_paths.py
--                                     -- no `detail` parameter
--     grep -rn "append_step(" server/core/*.py
--                                     -- ONE caller, ai_path_recorder.py:184
--
--   Story 54.2 builds the judged candidates of a crossing -- nine parallel lists
--   plus a walk descriptor (`core/candidate_emission.py:candidate_detail`) -- and
--   posts them on the live progress stream. `emit_candidates` returns early when
--   `emission_is_armed()` is false, so when nobody is watching the payload is not
--   even assembled. Nothing ever wrote it down.
--
--   The consequence is visible in the product and was stated honestly rather than
--   hidden: `ui/cards/shell/src/viz/renderers/aiPathBranches.tsx` resolves the
--   listing to `branches_not_recorded` and the screen says `branch count unknown`.
--   Migration 175 gave that state its own value precisely so it could not be read
--   as a zero. This migration is what lets a reader get a COUNT instead.
--
-- WHY A `detail` COLUMN AND NOT A `rationale` ONE -- migration 150 refused that.
--
--   150 wrote, and it was right: "There is no column for model reasoning, and that
--   absence is load-bearing -- a nullable `rationale` column is an invitation to
--   fill it with an inference."
--
--   `detail` is not that column, and the difference is enforceable rather than
--   intended. It is the SAME map the recorder already assembles and already puts
--   on the wire, and that map has carried a lock since Story 54.2:
--   `ai_path_recorder.BANNED_DETAIL_KEYS` refuses eighteen prose-shaped key names,
--   `DETAIL_VALUE_MAX_CHARS` drops any value over 200 characters rather than
--   truncating it ("truncated prose is still prose"), and `DETAIL_MAX_KEYS` caps
--   the map at 24 entries.
--
--   That lock lived in ONE Python function. A column guarded only by its single
--   caller is guarded until the second caller. So the lock is restated HERE, as a
--   CHECK, in `app.ai_path_detail_is_recordable`: the eighteen banned names, the
--   200 characters, the 24 keys, and no nested object at all. A future writer that
--   forgets `sanitize_detail` is refused by PostgreSQL, not by a convention.
--
--   Two copies of a list drift. `server/tests/conformance/test_ai_path_detail_lock.py`
--   pins this array to `BANNED_DETAIL_KEYS` by parsing both, so the second copy
--   cannot quietly fall behind the first.
--
-- WHAT THIS DOES NOT DO, STATED SO NOBODY RE-DERIVES IT.
--
--   * It does NOT persist the five reading levels. The arbitration of 2026-08-01
--     (`SESSIONS.md`) settled that the five rungs are a READING grid: no `level`
--     column, and none is added here. `step_kind` already carries what is stored.
--   * It does NOT enter `content_hash`. `finalize_path` hashes an enumerated field
--     list (`ai_paths.py:302-324`) that does not include `detail`, and it stays
--     that way: adding a field to the formula would make every ALREADY finalized
--     path recompute to a different digest and become unverifiable. Whether the
--     judged branches belong to the pinned identity of a walk is a real question;
--     it is a separate one, and answering it silently inside an ALTER TABLE is how
--     an integrity hash stops meaning what its readers think it means.
--
-- Additive, idempotent, replayable. No row is inserted: the column becomes
-- populated because a walk records what it judged, never to make a screen look
-- populated.
--
-- Schema-Change-Checklist (CONTRIBUTING.md):
--   [x] Additive & idempotent (IF NOT EXISTS / OR REPLACE; replayable)
--   [x] New column is NULL-able
--   [x] No destructive DROP/ALTER on populated columns
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. The lock, as a function, so the CHECK reads as one sentence and the twenty
--    banned names live in exactly one place on this side of the wire.
--
--    IMMUTABLE and STRICT: a CHECK constraint may only call an immutable
--    function, and STRICT makes a NULL `detail` trivially acceptable without a
--    branch (an absent map is the normal state of a step that judged nothing).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.ai_path_detail_is_recordable(detail JSONB)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
STRICT
AS $$
DECLARE
    entry   RECORD;
    element JSONB;
    banned  CONSTANT TEXT[] := ARRAY[
        'analysis', 'answer', 'chain_of_thought', 'commentary', 'completion',
        'content', 'explanation', 'message', 'narrative', 'note', 'notes',
        'prose', 'rationale', 'reasoning', 'summary', 'text', 'thought',
        'thoughts'
    ];
BEGIN
    -- A recorded detail is a handful of facts about one step, not a document.
    IF jsonb_typeof(detail) <> 'object' THEN
        RETURN FALSE;
    END IF;
    IF (SELECT count(*) FROM jsonb_object_keys(detail)) > 24 THEN
        RETURN FALSE;
    END IF;

    FOR entry IN SELECT key, value FROM jsonb_each(detail) LOOP
        IF lower(entry.key) = ANY (banned) THEN
            RETURN FALSE;
        END IF;

        CASE jsonb_typeof(entry.value)
            WHEN 'object' THEN
                -- Nesting is how prose re-enters a map that refuses prose keys.
                RETURN FALSE;
            WHEN 'string' THEN
                IF length(entry.value #>> '{}') > 200 THEN
                    RETURN FALSE;
                END IF;
            WHEN 'array' THEN
                IF jsonb_array_length(entry.value) > 24 THEN
                    RETURN FALSE;
                END IF;
                FOR element IN SELECT * FROM jsonb_array_elements(entry.value) LOOP
                    IF jsonb_typeof(element) IN ('object', 'array') THEN
                        RETURN FALSE;
                    END IF;
                    IF jsonb_typeof(element) = 'string'
                       AND length(element #>> '{}') > 200 THEN
                        RETURN FALSE;
                    END IF;
                END LOOP;
            ELSE
                NULL;  -- null / boolean / number are recordable as they are
        END CASE;
    END LOOP;

    RETURN TRUE;
END;
$$;

COMMENT ON FUNCTION app.ai_path_detail_is_recordable(JSONB) IS
    'The Story 54.2 recorded-detail shape, restated in the database. Mirrors '
    'ai_path_recorder.BANNED_DETAIL_KEYS / DETAIL_VALUE_MAX_CHARS / '
    'DETAIL_MAX_KEYS. It exists because that lock lived in one Python function, '
    'and a column guarded only by its single caller is guarded until the second '
    'caller.';

-- ---------------------------------------------------------------------------
-- 2. The column.
-- ---------------------------------------------------------------------------
ALTER TABLE app.ai_path_steps
    ADD COLUMN IF NOT EXISTS detail JSONB;

ALTER TABLE app.ai_path_steps
    DROP CONSTRAINT IF EXISTS ck_ai_path_steps_detail_recordable;
ALTER TABLE app.ai_path_steps
    ADD CONSTRAINT ck_ai_path_steps_detail_recordable
    CHECK (detail IS NULL OR app.ai_path_detail_is_recordable(detail));

COMMENT ON COLUMN app.ai_path_steps.detail IS
    'What this crossing judged, as the recorder already emits it: the Story 54.2 '
    'nine parallel candidate lists (candidate_ids / _kinds / _titles / _scores / '
    '_tiers / _matched / _ranks / _fates / _reasons) plus the walk descriptor '
    'scalars, decoded by decodeAiPathBranches. Parallel lists rather than a list '
    'of objects because the shape refuses nesting. NULL means the step judged '
    'nothing to record -- which a reader must show as `branches_not_recorded`, '
    'never as a zero (migration 175). Not part of content_hash, on purpose.';

COMMIT;
