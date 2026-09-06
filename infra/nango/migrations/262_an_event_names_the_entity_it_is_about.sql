-- 262 -- An event names the entity it is about.
--
-- THE MEASUREMENT. On proj_01KZGCRSV2XACWRP3RSVNWWGBK, 2026-08-14: the published
-- relation of the per-video Datastream carries 519 distinct values of the governed
-- dimension `video`, and app.context_events holds 3 rows. Those 3 carry a title in
-- `label` and a URL in `description` -- two free-text columns -- and nothing at all
-- that names WHICH of the 519 they are about.
--
-- The id is inside the URL (`...watch?v=b6wfcAYukFE`), and all three do match an
-- observed value, so the join exists in fact and cannot be expressed: extracting
-- it would mean a server that knows the URL shape of one provider, which AD-2
-- forbids and which would be wrong for the next domain anyway.
--
-- AND THE SEAM HAS NO FIELD FOR IT EITHER. `context_events.persist_context_event`
-- takes label, description, platform, value and source. `transform_events` of the
-- YouTube connector returns `{event_type, event_date, label, platform, source}`.
-- So a Connector that KNOWS its entity key -- every one of them does, it is the
-- key of the row it is transforming -- has nowhere to put it.
--
-- WHAT THIS ADDS, and why two columns rather than one:
--
--   entity_key   the value as the SOURCE writes it, so it compares to the
--                observed dimension without a rule in between;
--   entity_kind  what that key identifies, in the Connector's own vocabulary
--                (`video`, `product`, `campaign`). Without it, two event types
--                of one Connector whose keys are drawn from different sets would
--                collide the day their keys happen to look alike.
--
-- BOTH ARE NULLABLE, AND THAT IS THE POINT. An event that names no entity is a
-- legitimate event -- a holiday, a price change, a manual marker. What must never
-- happen is the third state: a product that cannot tell "this event is about
-- nothing in particular" from "this event is about something and we lost which".
-- A NULL is the first; there is no encoding for the second, because there is no
-- such event any more.
--
-- Contract: docs/product-architecture/data.md, "Amendment, chantier C".

BEGIN;

ALTER TABLE app.context_events
    ADD COLUMN IF NOT EXISTS entity_key  TEXT,
    ADD COLUMN IF NOT EXISTS entity_kind TEXT;

-- A key without a kind is a key nobody can compare safely, and a kind without a
-- key describes nothing. Either both or neither -- checked, not commented.
ALTER TABLE app.context_events
    DROP CONSTRAINT IF EXISTS ck_context_events_entity_pair;
ALTER TABLE app.context_events
    ADD CONSTRAINT ck_context_events_entity_pair CHECK (
        (entity_key IS NULL AND entity_kind IS NULL)
     OR (entity_key IS NOT NULL AND btrim(entity_key) <> ''
         AND entity_kind IS NOT NULL AND btrim(entity_kind) <> '')
    );

-- The inventory of holes reads (project, kind) and lists the keys that landed.
CREATE INDEX IF NOT EXISTS ix_context_events_entity
    ON app.context_events (project_id, entity_kind, entity_key)
    WHERE entity_key IS NOT NULL;

COMMENT ON COLUMN app.context_events.entity_key IS
    'The entity this event is about, as the SOURCE writes it -- so it compares to '
    'an observed dimension value with no rule in between. NULL means the event is '
    'about no particular entity, never that the entity was lost.';
COMMENT ON COLUMN app.context_events.entity_kind IS
    'What entity_key identifies, in the Connector''s own vocabulary (video, '
    'product, campaign). Keeps two key sets of one Connector from colliding.';

COMMIT;
