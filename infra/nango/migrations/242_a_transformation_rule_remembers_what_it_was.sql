-- Story 60.5: a transformation rule remembers what it was.
--
-- WHAT WAS MEASURED, AND WHY THIS MIGRATION EXISTS. `app.value_mapping_tables`
-- (235), `app.value_mapping_entries` (235) and `app.cleanup_rules` (240) carry
-- `created_at`/`updated_at` and no version column at all; the only triggers on
-- the three are `trg_*_updated_at`. The writes are UPDATEs in place --
-- `value_mapping_tables.py` (the table name/description, and what one pair
-- becomes) and `cleanup_rules.py` (the pattern). What the value became yesterday
-- was therefore lost, and nobody could date the change, in a schema that already
-- carries two hundred and eighteen immutability triggers and forty version
-- tables.
--
-- ONE LEDGER PER FAMILY, NOT ONE POLYMORPHIC LEDGER. A single
-- `transformation_rule_versions` table could not carry a foreign key to two
-- different parents, so its `object_type` column would be a claim the schema
-- cannot check. The seven version ledgers this repository already indexes in
-- `core/evidence_index.py` are all mono-object, and the factory that indexes
-- them (`_version_producer`) takes the table and the object column as
-- parameters: the cost of a second table is one registration, not a second
-- engine.
--
-- A VERSION IS PER TABLE, NEVER PER PAIR. `content_hash` covers the WHOLE body
-- -- the name, the description, the scope and every pair, sorted by source value
-- -- so importing a two-column file is ONE version rather than five hundred. The
-- same shape migration 145 gave a DQ Monitor version (145:337-377), including
-- its `UNIQUE (object, content_hash)`.
--
-- WHAT THAT UNIQUE MEANS HERE, SAID PRECISELY BECAUSE IT DIFFERS FROM 145. A DQ
-- Monitor version is a PUBLISHED policy and republishing an identical one is a
-- no-op, so 145 can refuse it outright. A cleanup rule and a value table are
-- edited freely, and returning a pattern from B back to A is a legitimate act
-- that a table-wide UNIQUE would make impossible. So the constraint is read here
-- as the IDENTITY OF A BODY rather than as a refusal: the ledger holds each
-- distinct body this object has ever carried, numbered in order of first
-- appearance, and `current_version_id` says which of them the object is at right
-- now. Returning to a body already recorded moves the pointer BACK to it and
-- records nothing new -- which is why no history row is ever written twice, and
-- why a version row never needs to be updated.
--
-- CONSEQUENCE, AND IT IS THE REASON THE TRIGGERS BELOW ARE ABSOLUTE. Because the
-- head is a pointer on the parent and never a status transition on a version
-- row, NO UPDATE of any kind is legitimate on either ledger. 145 had to allow
-- `published -> superseded`; here that exception does not exist and is not
-- offered. `status` is written once, at insert, and stays.
--
-- WHAT THE BODY DELIBERATELY EXCLUDES. `app.cleanup_rules.enabled` is NOT part
-- of the versioned body. Enabling and disabling a rule is its lifecycle, exactly
-- as `lifecycle_status` is the lifecycle of a DQ Monitor and lives on the parent
-- in 145 rather than inside the version. Putting it in the body would also make
-- `uq_cleanup_rule_version_content` refuse the third toggle of a rule that was
-- switched off and back on, which is not a governance decision -- it is an
-- accident of hashing a flag.
--
-- THE IMMUTABILITY TRIGGERS CARRY THE RGPD HATCH, AND THEY CARRY IT FROM BIRTH.
-- `current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on'` is the
-- condition migrations 098/099 established and 143, 146, 147, 149, 150 and 151
-- have written into every protective trigger since. Without it a new append-only
-- ledger BLOCKS an organization erasure -- `core/org_purge.py:329` sets exactly
-- that flag with SET LOCAL and expects every guard to stand down for it. No
-- conformance test in this repository demands the hatch of a FUTURE trigger, so
-- the guarantee is stated here and proven by
-- `server/tests/core/test_rule_version_rgpd_hatch.py`.
--
-- AND A VERSION DIES ONLY WITH ITS OBJECT. Deleting one value table or one
-- cleanup rule is a live client act (`delete_table`, `delete_rule`), so the
-- ledger rows hang off the parent with ON DELETE CASCADE. The trigger function
-- therefore allows a DELETE only when the parent row is already gone -- which is
-- true inside the cascade and false for every hand-written DELETE. Erasing one
-- recorded body while its object still exists is the single act an immutable
-- ledger exists to refuse, and it stays refused.
--
-- WHAT THIS MIGRATION DOES NOT DO, stated so no reader infers it:
--   * it does not re-edit 122, 145, 235 or 240;
--   * it does not make either store READ at render time. Nothing reads
--     `app.value_mapping_entries` or `app.cleanup_rules` on a report path today
--     and this migration changes none of that (AI-260);
--   * it builds no execution engine. `bounded_reprocess` still declares
--     `has_engine=False` in `core/run_origins.py`, and a rule applied at read
--     has nothing to replay: 122:23-30 wrote why -- "changing a regex costs
--     nothing -- no refetch, no 16-month backfill".
--
-- ORG SCOPING / RGPD. `org_id` is NOT NULL on both ledgers and its foreign key
-- is ON DELETE CASCADE, the same reach 235:54-61 and 240 measured for their own
-- tables: `core.org_purge.plan_purge` walks `confdeltype IN ('a','r')` only, so
-- a CASCADE edge is absent from its plan and the foreign key is what makes the
-- final `DELETE FROM app.organizations` reach these rows.
--
-- Idempotent: CREATE TABLE / INDEX IF NOT EXISTS, ALTER guarded, trigger
-- creation guarded by DROP TRIGGER IF EXISTS.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. The immutable history of one client value mapping table.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.value_mapping_table_versions (
    id                      TEXT        PRIMARY KEY,   -- prefixed ULID: 'vmtv_'
    table_id                TEXT        NOT NULL
                            REFERENCES app.value_mapping_tables(id) ON DELETE CASCADE,
    org_id                  TEXT        NOT NULL
                            REFERENCES app.organizations(id) ON DELETE CASCADE,
    -- Mirrors the scope of the table itself: NULL for an ORG-scoped table, which
    -- is visible to every Project of the organization (235). It is not defaulted
    -- to a Project, because an ORG table belongs to none.
    project_id              TEXT        REFERENCES app.projects(id) ON DELETE CASCADE,
    version_number          INTEGER     NOT NULL CHECK (version_number >= 1),
    status                  TEXT        NOT NULL
                            CHECK (status IN ('draft', 'published', 'superseded', 'archived')),
    -- The whole body: name, description, scope_level, and every pair sorted by
    -- source value. This is what "the old body is still readable" means.
    body                    JSONB       NOT NULL CHECK (jsonb_typeof(body) = 'object'),
    content_hash            TEXT        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    predecessor_version_id  TEXT        REFERENCES app.value_mapping_table_versions(id),
    created_by              TEXT        NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_value_mapping_table_version UNIQUE (table_id, version_number),
    -- 145:373, and for the same reason: re-recording a body this object already
    -- carries would put one state under two numbers and make the history lie
    -- about how many times it really changed.
    CONSTRAINT uq_value_mapping_table_version_content UNIQUE (table_id, content_hash),
    -- Version 1 has no predecessor; every later version names one. A revision
    -- that loses its lineage is indistinguishable from a fresh object (151:105-107).
    CONSTRAINT ck_value_mapping_table_version_lineage CHECK (
        (version_number = 1 AND predecessor_version_id IS NULL)
        OR (version_number > 1 AND predecessor_version_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_value_mapping_table_versions_head
    ON app.value_mapping_table_versions (table_id, version_number DESC);
CREATE INDEX IF NOT EXISTS idx_value_mapping_table_versions_project
    ON app.value_mapping_table_versions (project_id);

COMMENT ON TABLE app.value_mapping_table_versions IS
    'Story 60.5: the immutable history of one client value mapping table. One '
    'version per ACT -- an import of five hundred pairs is one row, because '
    'content_hash covers the whole body with its pairs sorted. Nothing reads this '
    'ledger at render time: story 60.5 delivers the history, not the application.';

COMMENT ON COLUMN app.value_mapping_table_versions.status IS
    'The four states of the 145:337-377 template are kept so this ledger reads '
    'like the seven beside it, but core/rule_versions.py writes ONLY "published" '
    'and never updates the row: which version is current is '
    'app.value_mapping_tables.current_version_id, a pointer on the parent.';

COMMENT ON COLUMN app.value_mapping_table_versions.body IS
    'name, description, scope_level and every pair as [source_value, '
    'canonical_value] sorted by source_value. content_hash is the sha256 of its '
    'canonical JSON, computed by core/rule_versions.py and by nothing else.';

-- ---------------------------------------------------------------------------
-- 2. The immutable history of one cleanup rule.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.cleanup_rule_versions (
    id                      TEXT        PRIMARY KEY,   -- prefixed ULID: 'crlv_'
    rule_id                 TEXT        NOT NULL
                            REFERENCES app.cleanup_rules(id) ON DELETE CASCADE,
    org_id                  TEXT        NOT NULL
                            REFERENCES app.organizations(id) ON DELETE CASCADE,
    -- NOT NULL here and nullable above, because the two parents differ: a
    -- cleanup rule is Project-scoped by construction (240), a value table may be
    -- ORG-scoped.
    project_id              TEXT        NOT NULL
                            REFERENCES app.projects(id) ON DELETE CASCADE,
    version_number          INTEGER     NOT NULL CHECK (version_number >= 1),
    status                  TEXT        NOT NULL
                            CHECK (status IN ('draft', 'published', 'superseded', 'archived')),
    -- name, source_field, rule_kind, pattern. `enabled` is NOT here -- see the
    -- header: it is the rule's lifecycle, not its content.
    body                    JSONB       NOT NULL CHECK (jsonb_typeof(body) = 'object'),
    content_hash            TEXT        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    predecessor_version_id  TEXT        REFERENCES app.cleanup_rule_versions(id),
    created_by              TEXT        NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_cleanup_rule_version UNIQUE (rule_id, version_number),
    CONSTRAINT uq_cleanup_rule_version_content UNIQUE (rule_id, content_hash),
    CONSTRAINT ck_cleanup_rule_version_lineage CHECK (
        (version_number = 1 AND predecessor_version_id IS NULL)
        OR (version_number > 1 AND predecessor_version_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_cleanup_rule_versions_head
    ON app.cleanup_rule_versions (rule_id, version_number DESC);
CREATE INDEX IF NOT EXISTS idx_cleanup_rule_versions_project
    ON app.cleanup_rule_versions (project_id);

COMMENT ON TABLE app.cleanup_rule_versions IS
    'Story 60.5: the immutable history of one cleanup rule -- its name, the field '
    'it reads, what it does and the pattern it does it with. `enabled` is '
    'deliberately absent from the body: switching a rule off is its lifecycle, '
    'and hashing it would make the UNIQUE content constraint refuse the third '
    'toggle of a rule that was switched off and back on.';

-- ---------------------------------------------------------------------------
-- 3. The pointer. Appending a version and advancing the head are one act.
-- ---------------------------------------------------------------------------

ALTER TABLE app.value_mapping_tables
    ADD COLUMN IF NOT EXISTS current_version_id TEXT;
ALTER TABLE app.cleanup_rules
    ADD COLUMN IF NOT EXISTS current_version_id TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_value_mapping_tables_current_version'
          AND conrelid = 'app.value_mapping_tables'::regclass
    ) THEN
        ALTER TABLE app.value_mapping_tables
            ADD CONSTRAINT fk_value_mapping_tables_current_version
            FOREIGN KEY (current_version_id)
            REFERENCES app.value_mapping_table_versions (id)
            DEFERRABLE INITIALLY IMMEDIATE;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_cleanup_rules_current_version'
          AND conrelid = 'app.cleanup_rules'::regclass
    ) THEN
        ALTER TABLE app.cleanup_rules
            ADD CONSTRAINT fk_cleanup_rules_current_version
            FOREIGN KEY (current_version_id)
            REFERENCES app.cleanup_rule_versions (id)
            DEFERRABLE INITIALLY IMMEDIATE;
    END IF;
END
$$;

COMMENT ON COLUMN app.value_mapping_tables.current_version_id IS
    'The head of app.value_mapping_table_versions for this table. NULL only for a '
    'table written before migration 242: there is no backfill, because inventing '
    'a version 1 would date a change that was never observed.';

COMMENT ON COLUMN app.cleanup_rules.current_version_id IS
    'The head of app.cleanup_rule_versions for this rule. NULL only for a rule '
    'written before migration 242 -- no backfill, for the same reason.';

-- ---------------------------------------------------------------------------
-- 4. Immutability, with the erasure hatch and the cascade exemption.
-- ---------------------------------------------------------------------------
--
-- Two functions rather than one generic one, because each has to name its own
-- parent table to answer "is this DELETE part of the parent's cascade?". A
-- dynamic EXECUTE would answer the same question less legibly and no faster.

CREATE OR REPLACE FUNCTION app.reject_value_mapping_table_version_mutation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        -- Inside ON DELETE CASCADE the parent row is already gone, and that is
        -- the ONLY circumstance in which erasing a recorded body is honest: the
        -- object it described no longer exists. A hand-written DELETE finds its
        -- parent alive and is refused.
        IF NOT EXISTS (
            SELECT 1 FROM app.value_mapping_tables t WHERE t.id = OLD.table_id
        ) THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION
            'value mapping table versions are immutable: a recorded body is never deleted while its table exists'
            USING ERRCODE = '23000';
    END IF;

    -- NO UPDATE AT ALL, and the header says why: the head of the history is
    -- `app.value_mapping_tables.current_version_id`, a pointer on the parent, so
    -- no legitimate act ever has to move a column of a recorded version.
    RAISE EXCEPTION
        'value mapping table versions are immutable: edit the table, which appends a new version'
        USING ERRCODE = '23000';
END;
$$;

DROP TRIGGER IF EXISTS trg_value_mapping_table_versions_immutable
    ON app.value_mapping_table_versions;
CREATE TRIGGER trg_value_mapping_table_versions_immutable
    BEFORE UPDATE OR DELETE ON app.value_mapping_table_versions
    FOR EACH ROW
    WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
    EXECUTE FUNCTION app.reject_value_mapping_table_version_mutation();

CREATE OR REPLACE FUNCTION app.reject_cleanup_rule_version_mutation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF NOT EXISTS (
            SELECT 1 FROM app.cleanup_rules r WHERE r.id = OLD.rule_id
        ) THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION
            'cleanup rule versions are immutable: a recorded body is never deleted while its rule exists'
            USING ERRCODE = '23000';
    END IF;

    RAISE EXCEPTION
        'cleanup rule versions are immutable: edit the rule, which appends a new version'
        USING ERRCODE = '23000';
END;
$$;

DROP TRIGGER IF EXISTS trg_cleanup_rule_versions_immutable
    ON app.cleanup_rule_versions;
CREATE TRIGGER trg_cleanup_rule_versions_immutable
    BEFORE UPDATE OR DELETE ON app.cleanup_rule_versions
    FOR EACH ROW
    WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
    EXECUTE FUNCTION app.reject_cleanup_rule_version_mutation();

COMMIT;
