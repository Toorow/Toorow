-- ============================================================================
-- 228: a crossing may carry its reasons.
--
-- WHY THIS EXISTS. Migration 176 capped a recorded detail map at 24 keys, and
-- `ai_path_recorder.DETAIL_MAX_KEYS` said the same number on the Python side. The
-- cap was never the problem. What the cap did when reached was:
-- `sanitize_detail` iterates the map in insertion order and `break`s at the
-- limit, so the TAIL is dropped -- silently, with no log line at all, unlike
-- every other refusal in that function.
--
-- Measured 2026-08-08 on the candidate crossing of story 54.2:
--
--     flattened descriptor           15 keys
--     candidates_listed + 9 lists    10 keys
--     total                          25 keys   for a budget of 24
--     the one key dropped            candidate_reasons
--
-- `candidate_reasons` is the enumerated motive a candidate was rejected -- the
-- single field that whole story exists to carry ("motif enumere et cite",
-- commit 1514f667). It vanished the day the retrieval descriptor grew a field,
-- and the three tests that assert it have been red on `main` ever since. Nothing
-- reported the loss: the crossing looked complete, minus its reasons.
--
-- WHAT CHANGES. The key budget becomes 32. Not because 24 was wrong, but because
-- an enumerated crossing of 25 facts is still "a handful of facts about one
-- step, not a document" -- the sentence migration 176 wrote and which this one
-- keeps. The eight-key headroom is what stops the next descriptor field from
-- silently costing another one.
--
-- WHAT DOES NOT CHANGE. The array cap stays 24, and that separation is half the
-- repair: one constant governed both the number of KEYS in a map and the number
-- of ITEMS in a list, so growing a descriptor shortened the candidate lists --
-- two budgets that have nothing to do with each other, moving together. Python
-- now names them apart (`DETAIL_MAX_KEYS` / `DETAIL_MAX_LIST_ITEMS`) and this
-- function keeps its two numbers distinct for the same reason.
--
-- The banned names, the 200-character value cap, and the refusal of nesting are
-- restated VERBATIM. `server/tests/conformance/test_ai_path_detail_lock.py`
-- parses this file and pins every number to its Python twin, and it now points
-- here rather than at 176 -- which is why 176 is not edited: an applied
-- migration is never re-opened, it is superseded.
--
-- Schema-Change-Checklist (CONTRIBUTING.md):
--   [x] Additive & idempotent (CREATE OR REPLACE; replayable)
--   [x] No column added, none altered, no row touched
--   [x] The CHECK it feeds only ever accepts MORE than it did
-- ============================================================================

BEGIN;

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
    IF (SELECT count(*) FROM jsonb_object_keys(detail)) > 32 THEN
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
                -- 24, and deliberately NOT the key cap above: how many candidates
                -- travel and how many facts describe them are two budgets.
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
    'DETAIL_MAX_KEYS / DETAIL_MAX_LIST_ITEMS. Migration 228 raised the key cap '
    'from 24 to 32 and kept the array cap at 24: one number had been governing '
    'both, so a descriptor that grew silently dropped candidate_reasons.';

COMMIT;
