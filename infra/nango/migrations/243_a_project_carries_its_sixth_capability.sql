-- Placement Mapping becomes the sixth Project capability (story 61.5).
--
-- Migration 131 wrote FIVE capability keys in two places that both refuse a
-- sixth: two CHECK constraints on app.project_capabilities, and the body of
-- app.seed_project_capabilities. 131 is applied and immutable, so this migration
-- corrects forward -- DROP ... IF EXISTS then ADD CONSTRAINT under the SAME
-- names PostgreSQL generated for the inline CHECKs, read from a live database
-- with pg_get_constraintdef. Guessing those names would have dropped nothing and
-- left the old constraint refusing the key beside a new one accepting it.
--
-- WHY THE BACKFILL IS HERE AND NOT ON THE ACTIVATION PATH. No statement in
-- server/core ever INSERTs into app.project_capabilities: prepare and confirm
-- both UPDATE, and neither reads the row count of that UPDATE. An activation
-- aimed at a row that does not exist therefore updates zero rows, raises
-- nothing, mints its Configuration Version and reports success -- a capability
-- that stays absent while the Change Set says it was turned on. Seeding every
-- existing Project here keeps app.seed_project_capabilities the single writer of
-- this table and makes that UPDATE touch exactly one row.
--
-- WHAT THIS MIGRATION DOES NOT DO, so nobody looks for it: it installs no
-- trigger, no immutability rule and no append-only journal, and therefore needs
-- no erasure hatch of the kind 099_rgpd_erasure_trigger_guards.sql adds. This
-- table stays a mutable posture row that core/org_purge.py already reaches
-- through the foreign-key graph, with no table list to maintain. It also does
-- not name the two columns Placement Mapping adds to an aggregation: those are
-- open in docs/product-architecture/project-settings.md and closed by story 61.1.

ALTER TABLE app.project_capabilities
    DROP CONSTRAINT IF EXISTS project_capabilities_capability_key_check;
ALTER TABLE app.project_capabilities
    ADD CONSTRAINT project_capabilities_capability_key_check
    CHECK (capability_key IN (
        'country', 'currency_fx', 'reporting_timezone', 'tax_fees', 'competitors',
        'placement_mapping'
    ));

-- The pairing CHECK is the one that is easy to miss: it re-enumerates every key
-- beside the availability it is allowed to carry, so a key accepted by the first
-- constraint and absent from this one is refused anyway. Placement Mapping is
-- `optional` -- docs/product-architecture/project-settings.md, ratified
-- 2026-08-05: "Placement Mapping | Optional and Disabled by default" -- so it
-- joins the second member and can never be written `always_present`.
--
-- The third CHECK (project_capabilities_check, NOT (always_present AND
-- disabled)) is deliberately untouched: `optional` + `disabled` is exactly what
-- this capability is born as, and that constraint already permits it.
ALTER TABLE app.project_capabilities
    DROP CONSTRAINT IF EXISTS project_capabilities_check1;
ALTER TABLE app.project_capabilities
    ADD CONSTRAINT project_capabilities_check1
    CHECK ((capability_key IN ('currency_fx', 'reporting_timezone')
            AND availability = 'always_present')
        OR (capability_key IN ('country', 'tax_fees', 'competitors', 'placement_mapping')
            AND availability = 'optional'));

CREATE OR REPLACE FUNCTION app.seed_project_capabilities(target_project_id TEXT)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO app.project_capabilities (project_id, capability_key, availability, state)
    VALUES
        (target_project_id, 'country', 'optional', 'disabled'),
        (target_project_id, 'currency_fx', 'always_present', 'draft'),
        (target_project_id, 'reporting_timezone', 'always_present', 'draft'),
        (target_project_id, 'tax_fees', 'optional', 'disabled'),
        (target_project_id, 'competitors', 'optional', 'disabled'),
        (target_project_id, 'placement_mapping', 'optional', 'disabled')
    ON CONFLICT (project_id, capability_key) DO NOTHING;
END;
$$;

-- The reprise. Idempotent through the ON CONFLICT above, so it adds the missing
-- sixth row to every Project that already carries five and the full six to a
-- Project that somehow carries none. trg_projects_seed_capabilities already
-- calls the same function for every Project created from now on.
SELECT app.seed_project_capabilities(id) FROM app.projects;
