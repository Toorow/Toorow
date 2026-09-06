-- 212 — an Event observation binding cannot be undone, or forged, by an UPDATE.
--
-- AI-191. Migration 133 posed `app.require_event_observation_binding` as a
-- BEFORE **INSERT** trigger only. An UPDATE went straight past it, so the
-- application role `connector` could set `event_configuration_version_id` back
-- to NULL on a bound row and then write `binding_state = 'linked'` by hand.
-- Verified on a disposable cluster: nothing refused it.
--
-- This is NOT a proven product defect — no known caller performs that UPDATE.
-- What was proven is that nothing prevented it, and `binding_state` is what
-- `resolve_event_observation_references` (Story 49.6 AC9) reads to decide
-- whether an observation may point at a Datastream at all. A column that
-- decides a disclosure must not be writable into a lie.
--
-- WHY A SECOND FUNCTION, AND NOT THE 133 ONE ON UPDATE. Reusing
-- `require_event_observation_binding` for UPDATE would refuse every update to a
-- legacy row that is still unbound — and those rows are exactly the ones 133
-- left behind, because it bound only observations whose owning Datastream was
-- provable (a single one). Touching such a row's payload would start raising
-- 23514, which is a new refusal, not a preserved invariant. The invariant this
-- migration protects is narrower and is the real one:
--
--   1. a binding, once made, is never unmade — the two columns of a bound row
--      may not go back to NULL, and may not be repointed at another version or
--      another Datastream;
--   2. `binding_state` is DERIVED, never asserted — it is recomputed from the
--      columns on every write, so an unbound row cannot claim `linked`.
--
-- Rule 2 also closes the INSERT half by the same statement: 133 forced
-- `linked` on insert, which was correct only because insert requires a binding.
--
-- The trigger sits on a table the Data surface owns and the overlay only reads,
-- so it is posed as a SEPARATE trigger with its own name: the 133 one keeps its
-- meaning (a new observation MUST arrive bound), this one keeps its own (a
-- binding is immutable and its state is a function of it). A migration is never
-- re-edited (CLAUDE.md); the correction comes in the next one.

BEGIN;

CREATE OR REPLACE FUNCTION app.freeze_event_observation_binding()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    -- 1) A binding is never unmade, and never repointed.
    IF OLD.event_configuration_version_id IS NOT NULL THEN
        IF NEW.event_configuration_version_id IS DISTINCT FROM OLD.event_configuration_version_id
           OR NEW.datastream_id IS DISTINCT FROM OLD.datastream_id THEN
            RAISE EXCEPTION
                'an event observation binding is immutable: version % / datastream % cannot become % / %',
                OLD.event_configuration_version_id, OLD.datastream_id,
                NEW.event_configuration_version_id, NEW.datastream_id
                USING ERRCODE = '23514';
        END IF;
    END IF;

    -- 2) `binding_state` is derived from the columns, never from the writer.
    --    A row that carries both halves of the pointer is linked; anything else
    --    is unavailable, which is the exact word AC9 discloses.
    IF NEW.event_configuration_version_id IS NOT NULL AND NEW.datastream_id IS NOT NULL THEN
        NEW.binding_state = 'linked';
    ELSE
        NEW.binding_state = 'unavailable';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_context_events_freeze_binding ON app.context_events;
CREATE TRIGGER trg_context_events_freeze_binding
    BEFORE UPDATE ON app.context_events
    FOR EACH ROW EXECUTE FUNCTION app.freeze_event_observation_binding();

COMMIT;
