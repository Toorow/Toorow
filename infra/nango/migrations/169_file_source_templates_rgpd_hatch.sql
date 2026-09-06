-- app.file_source_templates gets the RGPD erasure hatch it was born without.
--
-- WHY, AND HOW IT WAS FOUND. The light control pass of 2026-07-31 (Story 22.11)
-- measured the production database and found exactly one row:
--
--     SELECT id, project_id, template_code, created_by, created_at
--       FROM app.file_source_templates;
--     -- fst_01KYDN9VKCDRAV197DXJ4NDVWV | proj_6d30bef873ba | <a real client name>
--     -- | created_by = 'tester' | 2026-07-25 22:11:02Z
--
-- That is the fixture of `server/tests/integration/test_file_source_template_pg.py`,
-- written into production by a pg-gated suite whose TEST_POSTGRES_DSN pointed at
-- Supabase. It is the anti-drift rule n10 of CLAUDE.md, replayed: the 29 test
-- Datastreams had produced exactly this shape, and their plan versions are still
-- undeletable today.
--
-- WHAT MADE IT PERMANENT. Migration 097 gave this table an append-only trigger
-- (`app.reject_file_source_template_mutation`) that refuses every DELETE, with no
-- escape. Migration 098 had already established the idiom for the whole org tree:
-- UPDATE stays blocked unconditionally, and DELETE is allowed ONLY inside a
-- transaction that flags itself with `SET LOCAL app.rgpd_erasure = 'on'` -- the
-- flag `core/org_purge.py` sets, transaction-scoped, unable to leak to another
-- statement or session. 097 landed one day before 098 and never got it.
--
-- So this is not a special case written for one polluted row. It is the class:
-- every append-only table inside the org tree must be reachable by an audited
-- tenant erasure, or an RGPD request cannot be honoured. `app.file_source_templates`
-- was the one that was not.
--
-- WHAT THIS MIGRATION DOES NOT DO, said plainly so nobody reads more into it:
--
--   * It does NOT weaken immutability. UPDATE of the identity fields still raises
--     unconditionally -- append-only means history is not REWRITABLE, and erasure
--     of a whole tenant is a different, audited operation. Only `label` and
--     `is_active` remain updatable, exactly as before.
--   * It does NOT delete the polluted row. The hatch makes it REACHABLE by the
--     audited path; removing it is a separate, deliberate act. And the ordinary
--     purge will not reach it on its own: `project_id` is `TEXT NOT NULL` with
--     ZERO foreign key (097:46), the project `proj_6d30bef873ba` no longer exists,
--     so the row sits outside the FK tree `org_purge.py` walks. It is an orphan,
--     and an orphan needs a named statement, not a cascade.
--   * It does NOT add the missing foreign key. Adding one now would fail on this
--     very row, and repairing data from a migration that also changes structure
--     is how a migration becomes impossible to re-run. That belongs to a later
--     migration, after the orphan is resolved.
--
-- Idempotent: CREATE OR REPLACE + DROP TRIGGER IF EXISTS, safe to re-run.

BEGIN;

CREATE OR REPLACE FUNCTION app.reject_file_source_template_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql AS
$$
BEGIN
    IF TG_OP = 'DELETE' THEN
        -- The ONLY relaxation introduced by migration 169: an audited tenant
        -- erasure may remove the row. Everything else still raises.
        IF current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on' THEN
            RAISE EXCEPTION
                'file_source_templates is append-only: '
                'DELETE of id=% is forbidden (immutable template version). '
                'An RGPD tenant erasure must run inside a transaction that sets '
                'SET LOCAL app.rgpd_erasure = ''on'' (see migration 098).',
                OLD.id;
        END IF;
        RETURN OLD;
    END IF;
    -- UPDATE: freeze the identity fields; allow only label + is_active changes.
    -- UNCHANGED by 169, and deliberately NOT covered by the erasure flag.
    IF NEW.id                   IS DISTINCT FROM OLD.id                   OR
       NEW.project_id           IS DISTINCT FROM OLD.project_id           OR
       NEW.template_code        IS DISTINCT FROM OLD.template_code        OR
       NEW.version              IS DISTINCT FROM OLD.version              OR
       NEW.kind                 IS DISTINCT FROM OLD.kind                 OR
       NEW.content_hash         IS DISTINCT FROM OLD.content_hash         OR
       NEW.idempotency_key_hash IS DISTINCT FROM OLD.idempotency_key_hash OR
       NEW.contract             IS DISTINCT FROM OLD.contract             OR
       NEW.placement_class      IS DISTINCT FROM OLD.placement_class      OR
       NEW.grain                IS DISTINCT FROM OLD.grain                OR
       NEW.created_by           IS DISTINCT FROM OLD.created_by           OR
       NEW.created_at           IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION
            'file_source_templates: identity fields are immutable after insert '
            '(id=%). Only label and is_active may be updated.',
            OLD.id;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_file_source_template_immutable
    ON app.file_source_templates;

CREATE TRIGGER trg_file_source_template_immutable
    BEFORE UPDATE OR DELETE ON app.file_source_templates
    FOR EACH ROW
    EXECUTE FUNCTION app.reject_file_source_template_mutation();

COMMENT ON TABLE app.file_source_templates IS
    'Append-only file-source template versions (Story 22.11). UPDATE of the '
    'identity fields raises unconditionally; only label and is_active are '
    'mutable. DELETE raises too, EXCEPT inside a transaction that sets '
    'SET LOCAL app.rgpd_erasure = ''on'' -- the audited tenant-erasure path of '
    'migration 098, which this table was missing until migration 169. '
    'Known gap, deliberately left to a later migration: project_id carries no '
    'foreign key, so a row can outlive its project and escape the purge tree.';

COMMIT;
