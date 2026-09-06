-- 287 -- A manual annotation was never an observation, and stopped being writable.
--
-- THE MEASUREMENT, on the disposable cluster at ledger head 285, 2026-08-17:
--
--   INSERT INTO app.context_events
--       (id, project_id, event_date, type, label, description, created_by, source)
--   VALUES ('evt_REPRO_MANUAL', 'proj_EXAMPLE', DATE '2026-08-17',
--           'business', 'Price change', '', 'owner@example.com', 'manual');
--   -- ERROR 23514: new event observations require a Datastream-owned Event
--   --              Configuration version
--
-- So EVERY manual context event insert has been refused since migration 133.
-- Both writers of a manual row die on it: the MCP tool `add_context_event`
-- (`core/context_hub_mcp.py`) and `POST /api/context-events`
-- (`core/context_events_api.py:42`), each surfacing it as an opaque `db_error`.
-- The audit of 2026-08-17 recorded those two routes as "orphaned — no console
-- caller". They were worse than unused: the creation path they serve could not
-- have succeeded for any caller, which is the likeliest reason nothing was ever
-- built on top of them.
--
-- WHY 133 IS NOT WRONG, AND WHAT IT MISSED. Migration 133 made every event
-- observation arrive BOUND to the Event Configuration version that produced it,
-- so an observation can never claim a Datastream it cannot prove. That invariant
-- is right and is preserved here untouched. What the trigger did not carry is
-- the distinction the writer already makes and states in prose:
-- `context_events._active_event_binding` returns NULL for `source = 'manual'`
-- with the comment "A manual event has no Connector and stays unbound." An
-- observation is emitted BY a Connector; a manual annotation is written by a
-- person about a day. The trigger applied the observation rule to both, and the
-- half that has no Connector to bind to could no longer be written at all.
--
-- The clause 133 should have carried is therefore a scope, not an exception:
-- the binding requirement governs rows a Connector emitted. Migration 009
-- already made `source` the column that separates them ('manual' vs the
-- Connector's name), and `delete_connector_events_in_window` has keyed on that
-- same column since Epic 31 for the same reason.
--
-- BINDING_STATE FOR A MANUAL ROW IS 'unavailable', NOT 'linked'. 133 forced
-- 'linked' on insert, which was only correct because insert required a binding.
-- Migration 212 already settled the general rule for UPDATE -- `binding_state`
-- is DERIVED from the two pointer columns, never asserted by the writer -- and
-- this brings INSERT under the same rule rather than posing a third one. A
-- manual row carries neither pointer half, so it is 'unavailable': the exact
-- word `resolve_event_observation_references` (Story 49.6 AC9) discloses, and
-- the honest one. Calling it 'linked' would let a manual annotation claim a
-- Datastream that does not exist.
--
-- NO BACKFILL, AND THAT IS A MEASUREMENT RATHER THAN AN OMISSION. Nothing needs
-- repairing behind this: the rows that would have been wrong are precisely the
-- rows the trigger refused to create. Manual rows predating 133 were bound by
-- 133's own backfill only where a single owning Datastream was provable, and
-- 212 froze those bindings; this migration does not touch an existing row.
--
-- A migration is never re-edited (CLAUDE.md): 133 keeps its meaning, and the
-- correction arrives here.
--
-- Contract: docs/product-architecture/context-hub.md, amendment of 2026-08-17
-- ("a manual context event has a human life").

BEGIN;

CREATE OR REPLACE FUNCTION app.require_event_observation_binding()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    -- A manual annotation has no Connector, so there is no Event Configuration
    -- version for it to point at. It is not an unbound observation; it is not an
    -- observation. It goes in with both pointer halves NULL, and says so.
    IF NEW.source = 'manual' THEN
        IF NEW.event_configuration_version_id IS NOT NULL
           OR NEW.datastream_id IS NOT NULL THEN
            -- A manual row that arrives carrying a binding is a caller
            -- attributing a person's annotation to a Datastream's collection.
            -- Refused rather than silently cleared: dropping the pointer would
            -- hide a caller that believes something false about its own write.
            RAISE EXCEPTION
                'a manual context event names no Event Configuration: % arrived bound to version % / datastream %',
                NEW.id, NEW.event_configuration_version_id, NEW.datastream_id
                USING ERRCODE = '23514';
        END IF;
        NEW.binding_state = 'unavailable';
        RETURN NEW;
    END IF;

    -- Everything a Connector emits keeps 133's invariant, unchanged: an
    -- observation arrives bound to the version that produced it, or not at all.
    IF NEW.event_configuration_version_id IS NULL OR NEW.datastream_id IS NULL THEN
        RAISE EXCEPTION 'new event observations require a Datastream-owned Event Configuration version'
            USING ERRCODE = '23514';
    END IF;
    NEW.binding_state = 'linked';
    RETURN NEW;
END;
$$;

-- The trigger itself is unchanged (same name, same timing, same table): only the
-- function body it already points at is replaced, so nothing re-binds and no
-- other trigger's order moves.

COMMENT ON FUNCTION app.require_event_observation_binding() IS
    'BEFORE INSERT on app.context_events. A Connector observation must arrive '
    'bound to the Event Configuration version that produced it (migration 133). '
    'A manual annotation has no Connector, arrives unbound, and is recorded '
    'binding_state = ''unavailable'' (migration 287) -- it was refused outright '
    'between 133 and 287.';

COMMIT;
