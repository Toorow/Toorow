-- Story 49.2: one Master Data authority, reached by extending 140 rather than
-- replacing it.
--
-- 49.2's Implementation Gate was written against migration 140 while it was
-- still unapplied, and told this story to reshape it. 140 is applied, and 141
-- already corrected two of the three findings forward. This is the third, plus
-- what 49.2 adds on top. Nothing below re-edits an applied migration.
--
-- The one modelling question 140 leaves open, and the answer taken here:
--
--   140 versions the REGISTRY. Publishing Country means publishing the whole
--   grouping atomically -- which is right for Country, where "France = FR + MC
--   + the DOM-TOM you attached" is one decision that must not land in halves.
--
--   49.2 needs Business Domains, Products and Activities to version PER
--   OBJECT. Publishing one Product must not mint a new version of every other
--   Product, and must not require re-approving them.
--
-- Two shapes, and both are in the ratified contract: `registry` and
-- `master-data-object` are distinct object types in the Story 49.1 route
-- registry. The wrong repairs would be to give Country a per-object lifecycle
-- (its grouping stops being atomic) or to give Products a registry-wide one
-- (one edit re-publishes the catalogue). The repair taken is neither: the
-- registry DECLARES its version scope, and the same five tables carry both.
--
-- What lands:
--
--   1. `version_scope` -- 'registry' (140's behaviour, the default, unchanged
--      for Country) or 'node' (one lifecycle per identity).
--   2. Scope -- platform / organization / project. 141 recorded this as
--      "Story 49.2 owns the org-scoped object model"; this is that.
--   3. `master_data_type_versions` -- client-defined object types with an
--      immutable JSON Schema Draft 2020-12 property schema and typed
--      relationship definitions. No enum, no migration to add a type.
--   4. `master_data_aliases` -- versioned mappings with an explicit SKOS
--      relation, because "alias" alone cannot say whether two labels are the
--      same thing or merely related.
--   5. `master_data_project_associations` -- an organization object REUSED by
--      a Project with a role, never copied into a Project master.
--   6. The RGPD erasure hatch, which 140 broke without anyone noticing.
--
-- Additive and idempotent. No table is dropped and no row is deleted.

BEGIN;

-- ---------------------------------------------------------------------------
-- 0. The RGPD erasure hatch, first, because it is a live defect.
--
-- Migration 098/099 established that an org erasure runs with
-- `app.rgpd_erasure = on` and that every protective DELETE trigger on an
-- org-scoped table must yield to that flag -- through an EXPLICIT allowlist,
-- so that adding a guarded table is a decision and not an accident.
--
-- 140 added four guarded tables and joined no allowlist. `org_purge` walks the
-- FK graph, so it FINDS master_data_object_versions and master_data_memberships
-- on its way to deleting a project -- and their triggers raise
-- "published master data versions are never deleted". An organization with one
-- published Country grouping could no longer be erased. Nobody would learn that
-- until a real erasure request arrived.
--
-- Note which tables are NOT here: the vocabulary and preset versions are
-- platform-scoped, belong to no tenant, and no org erasure has any business
-- deleting them -- the same reasoning 099 applied to import_templates.
-- ---------------------------------------------------------------------------
DO $migration$
DECLARE
    target_tables CONSTANT text[] := ARRAY[
        'master_data_object_versions',
        'master_data_memberships'
    ];
    guard CONSTANT text :=
        ' WHEN (current_setting(''app.rgpd_erasure'', true) IS DISTINCT FROM ''on'') ';
    rec record;
    new_def text;
BEGIN
    FOR rec IN
        SELECT c.relname AS tbl, t.tgname, pg_get_triggerdef(t.oid) AS def
        FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'app'
          AND c.relname = ANY (target_tables)
          AND NOT t.tgisinternal
    LOOP
        -- Already carrying the hatch: leave it exactly as it is.
        CONTINUE WHEN rec.def LIKE '%rgpd_erasure%';
        new_def := replace(rec.def, ' EXECUTE FUNCTION ', guard || ' EXECUTE FUNCTION ');
        IF new_def = rec.def THEN
            RAISE EXCEPTION 'could not add the erasure hatch to %.%', rec.tbl, rec.tgname;
        END IF;
        EXECUTE format('DROP TRIGGER %I ON app.%I', rec.tgname, rec.tbl);
        EXECUTE new_def;
    END LOOP;
END
$migration$;

-- ---------------------------------------------------------------------------
-- 1. Version scope. 'registry' is 140's behaviour and stays the default, so
--    Country is untouched by this migration.
-- ---------------------------------------------------------------------------
ALTER TABLE app.master_data_registries
    ADD COLUMN IF NOT EXISTS version_scope TEXT NOT NULL DEFAULT 'registry';

ALTER TABLE app.master_data_registries
    DROP CONSTRAINT IF EXISTS ck_master_data_registries_version_scope;
ALTER TABLE app.master_data_registries
    ADD CONSTRAINT ck_master_data_registries_version_scope
    CHECK (version_scope IN ('registry', 'node'));

COMMENT ON COLUMN app.master_data_registries.version_scope IS
    'Story 49.2. registry: the whole grouping publishes as one version (Country). node: each identity has its own lifecycle (Business Domains, Products, Activities).';

-- A version may name the single identity it belongs to. NULL keeps 140's
-- meaning exactly: the version is the registry's.
ALTER TABLE app.master_data_object_versions
    ADD COLUMN IF NOT EXISTS node_id TEXT;

ALTER TABLE app.master_data_object_versions
    DROP CONSTRAINT IF EXISTS fk_master_data_versions_node;
ALTER TABLE app.master_data_object_versions
    ADD CONSTRAINT fk_master_data_versions_node
    FOREIGN KEY (project_id, node_id)
    REFERENCES app.master_data_nodes(project_id, id) ON DELETE RESTRICT;

-- 140's `uq_master_data_versions_single_current` says one current version per
-- registry. Under node scope that is wrong -- every Product would compete for
-- the same slot. Replace it with two partial indexes that mean the same thing
-- at each scope, so neither can be violated.
DROP INDEX IF EXISTS app.uq_master_data_versions_single_current;

CREATE UNIQUE INDEX IF NOT EXISTS uq_master_data_versions_current_registry
    ON app.master_data_object_versions (project_id, registry_id)
    WHERE status = 'current' AND node_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_master_data_versions_current_node
    ON app.master_data_object_versions (project_id, node_id)
    WHERE status = 'current' AND node_id IS NOT NULL;

-- version_number is unique per registry in 140; under node scope it must count
-- per identity, or the second Product's first version is rejected as a
-- duplicate of the first Product's.
--
-- The constraint is found by its COLUMN SET, not by a guessed name: Postgres
-- truncates generated names at 63 characters, and this one is over.
DO $migration$
DECLARE
    victim text;
BEGIN
    SELECT con.conname INTO victim
    FROM pg_constraint con
    JOIN pg_class c ON c.oid = con.conrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'app'
      AND c.relname = 'master_data_object_versions'
      AND con.contype = 'u'
      AND (
          -- `attname` is `name`, not `text`; without the cast the comparison
          -- raises "operator does not exist: name[] = text[]".
          SELECT array_agg(att.attname::text ORDER BY att.attname::text)
          FROM unnest(con.conkey) AS k(attnum)
          JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = k.attnum
      ) = ARRAY['project_id', 'registry_id', 'version_number'];
    IF victim IS NOT NULL THEN
        EXECUTE format('ALTER TABLE app.master_data_object_versions DROP CONSTRAINT %I', victim);
    END IF;
END
$migration$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_master_data_versions_number_registry
    ON app.master_data_object_versions (project_id, registry_id, version_number)
    WHERE node_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_master_data_versions_number_node
    ON app.master_data_object_versions (project_id, node_id, version_number)
    WHERE node_id IS NOT NULL;

COMMENT ON COLUMN app.master_data_object_versions.node_id IS
    'Story 49.2. NULL: this version is the registry grouping (140, Country). Set: this version is one identity''s own history (Products, Activities, Business Domains).';

-- ---------------------------------------------------------------------------
-- 2. Scope. An organization object is reused by a Project through an explicit
--    association (section 5), never copied into a Project master. Making
--    project_id nullable is what allows the organization row to exist once.
-- ---------------------------------------------------------------------------
ALTER TABLE app.master_data_registries
    ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT 'project';
ALTER TABLE app.master_data_registries
    DROP CONSTRAINT IF EXISTS ck_master_data_registries_scope;
ALTER TABLE app.master_data_registries
    ADD CONSTRAINT ck_master_data_registries_scope
    CHECK (scope IN ('platform', 'organization', 'project'));

ALTER TABLE app.master_data_registries ALTER COLUMN project_id DROP NOT NULL;
ALTER TABLE app.master_data_nodes ALTER COLUMN project_id DROP NOT NULL;
ALTER TABLE app.master_data_object_versions ALTER COLUMN project_id DROP NOT NULL;

-- 140 links a node to its registry through (project_id, registry_id). A
-- composite foreign key is MATCH SIMPLE: the moment project_id is NULL the
-- whole key stops being checked, so an organization-scoped node could name a
-- registry that does not exist. The composite key still does its real job --
-- proving the two rows belong to the SAME Project -- and a single-column key
-- alongside it keeps the link itself enforced at every scope.
ALTER TABLE app.master_data_nodes
    DROP CONSTRAINT IF EXISTS fk_master_data_nodes_registry_any_scope;
ALTER TABLE app.master_data_nodes
    ADD CONSTRAINT fk_master_data_nodes_registry_any_scope
    FOREIGN KEY (registry_id) REFERENCES app.master_data_registries(id) ON DELETE RESTRICT;

ALTER TABLE app.master_data_object_versions
    DROP CONSTRAINT IF EXISTS fk_master_data_versions_registry_any_scope;
ALTER TABLE app.master_data_object_versions
    ADD CONSTRAINT fk_master_data_versions_registry_any_scope
    FOREIGN KEY (registry_id) REFERENCES app.master_data_registries(id) ON DELETE RESTRICT;

ALTER TABLE app.master_data_object_versions
    DROP CONSTRAINT IF EXISTS fk_master_data_versions_node_any_scope;
ALTER TABLE app.master_data_object_versions
    ADD CONSTRAINT fk_master_data_versions_node_any_scope
    FOREIGN KEY (node_id) REFERENCES app.master_data_nodes(id) ON DELETE RESTRICT;

-- A Project-scoped registry has a Project; an organization-scoped one must not,
-- or the same object would answer to two owners.
ALTER TABLE app.master_data_registries
    DROP CONSTRAINT IF EXISTS ck_master_data_registries_scope_project;
ALTER TABLE app.master_data_registries
    ADD CONSTRAINT ck_master_data_registries_scope_project
    CHECK ((scope = 'project') = (project_id IS NOT NULL));

-- 140's UNIQUE (project_id, object_kind) does not constrain organization rows,
-- because NULL never equals NULL. One owner per kind at each scope, stated
-- twice because the two scopes are two different uniqueness questions.
CREATE UNIQUE INDEX IF NOT EXISTS uq_master_data_registries_org_kind
    ON app.master_data_registries (org_id, object_kind)
    WHERE project_id IS NULL;

COMMENT ON COLUMN app.master_data_registries.scope IS
    'Story 49.2. organization: reused by Projects through master_data_project_associations. project: owned by one Project. platform: shipped vocabulary, never tenant-owned.';

-- A hierarchy edge follows its version's scope: an organization Business Domain
-- tree has no Project. `master_data_used_by` deliberately keeps its NOT NULL --
-- a consumer always lives in exactly one Project, whatever the scope of the
-- object it depends on.
ALTER TABLE app.master_data_memberships ALTER COLUMN project_id DROP NOT NULL;

ALTER TABLE app.master_data_memberships
    DROP CONSTRAINT IF EXISTS fk_master_data_memberships_version_any_scope;
ALTER TABLE app.master_data_memberships
    ADD CONSTRAINT fk_master_data_memberships_version_any_scope
    FOREIGN KEY (version_id) REFERENCES app.master_data_object_versions(id) ON DELETE CASCADE;

ALTER TABLE app.master_data_memberships
    DROP CONSTRAINT IF EXISTS fk_master_data_memberships_parent_any_scope;
ALTER TABLE app.master_data_memberships
    ADD CONSTRAINT fk_master_data_memberships_parent_any_scope
    FOREIGN KEY (parent_node_id) REFERENCES app.master_data_nodes(id) ON DELETE RESTRICT;

ALTER TABLE app.master_data_memberships
    DROP CONSTRAINT IF EXISTS fk_master_data_memberships_child_any_scope;
ALTER TABLE app.master_data_memberships
    ADD CONSTRAINT fk_master_data_memberships_child_any_scope
    FOREIGN KEY (child_node_id) REFERENCES app.master_data_nodes(id) ON DELETE RESTRICT;

-- The open-membership uniqueness of 140 is keyed on (project_id, version_id,
-- child). With project_id NULL those indexes stop constraining anything, so the
-- same rule is restated on the version alone -- which is where it always
-- belonged: one effective home per child INSIDE ONE VERSION.
CREATE UNIQUE INDEX IF NOT EXISTS uq_master_data_membership_open_value_any_scope
    ON app.master_data_memberships (version_id, child_value)
    WHERE child_value IS NOT NULL AND effective_to IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_master_data_membership_open_node_any_scope
    ON app.master_data_memberships (version_id, child_node_id)
    WHERE child_node_id IS NOT NULL AND effective_to IS NULL;

-- ---------------------------------------------------------------------------
-- 3. Object types. A client adds a Product subtype, or an object type nobody
--    anticipated, without a migration, an enum or a navigation change.
--
--    Immutable from birth, like the vocabulary and preset versions 140 already
--    treats that way: an object version pins the type version it validated
--    against, and a schema that could be edited underneath it would make an old
--    snapshot unreadable -- which AC3 forbids in as many words.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.master_data_type_versions (
    id TEXT PRIMARY KEY CHECK (id ~ '^mdtyp_[0-9A-HJKMNP-TV-Z]{26}$'),

    -- Organization-owned; a platform template has no org and is offered to all.
    org_id TEXT REFERENCES app.organizations(id) ON DELETE RESTRICT,
    object_kind TEXT NOT NULL CHECK (object_kind ~ '^[a-z][a-z0-9_]{1,39}$'),
    version_number INTEGER NOT NULL CHECK (version_number > 0),

    label TEXT NOT NULL CHECK (length(btrim(label)) BETWEEN 1 AND 120),
    description TEXT NOT NULL DEFAULT '',

    -- JSON Schema Draft 2020-12. Validated by the service before insert; the
    -- repository already depends on `jsonschema`, so no new library.
    property_schema JSONB NOT NULL CHECK (jsonb_typeof(property_schema) = 'object'),

    -- Ordering, grouping and widget hints. Presentation, never validation.
    display_hints JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(display_hints) = 'object'),

    -- [{key, label, target_kind, direction, cardinality, hierarchy,
    --   aggregation, effective_dated}]. `hierarchy` is 'tree' | 'dag' | 'flat';
    -- `aggregation` is 'additive' | 'navigation_only'. A navigation-only scheme
    -- makes NO analytical claim, which is the difference AC4 turns on.
    relationship_definitions JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(relationship_definitions) = 'array'),

    -- Where this type came from: 'platform_template', 'connector_template' or
    -- 'client'. A connector-proposed type records its contract fingerprint and
    -- then owes the connector nothing (AC8).
    origin TEXT NOT NULL DEFAULT 'client'
        CHECK (origin IN ('platform_template', 'connector_template', 'client')),
    origin_reference TEXT,
    origin_fingerprint TEXT
        CHECK (origin_fingerprint IS NULL OR origin_fingerprint ~ '^[0-9a-f]{64}$'),

    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (org_id, object_kind, version_number)
);

CREATE INDEX IF NOT EXISTS idx_master_data_type_versions_kind
    ON app.master_data_type_versions (object_kind, version_number DESC);

-- Deterministic re-import: the same type content resolves to the same row
-- instead of minting a rival definition, exactly as the vocabulary does.
CREATE UNIQUE INDEX IF NOT EXISTS uq_master_data_type_versions_content
    ON app.master_data_type_versions (COALESCE(org_id, ''), object_kind, content_hash);

CREATE OR REPLACE FUNCTION app.reject_master_data_type_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'master data type versions are immutable';
END;
$$;
DROP TRIGGER IF EXISTS trg_master_data_types_immutable
    ON app.master_data_type_versions;
CREATE TRIGGER trg_master_data_types_immutable
    BEFORE UPDATE OR DELETE ON app.master_data_type_versions
    FOR EACH ROW
    WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
    EXECUTE FUNCTION app.reject_master_data_type_mutation();

-- An object version records the type it validated against, so a historical
-- read resolves the schema of its own time rather than today's.
ALTER TABLE app.master_data_object_versions
    ADD COLUMN IF NOT EXISTS type_version_id TEXT
        REFERENCES app.master_data_type_versions(id) ON DELETE RESTRICT;

COMMENT ON TABLE app.master_data_type_versions IS
    'Story 49.2: client-defined object types. Immutable because an object version pins one, and a schema edited underneath a published snapshot would make it unreadable.';

-- ---------------------------------------------------------------------------
-- 4. Aliases. SKOS makes the distinction the word "alias" hides: `exact` says
--    two labels name the same thing, `related` says they merely travel
--    together, and `negative` says a match is explicitly refused. Collapsing
--    them is how an inbound matcher silently promotes "related" to "the same".
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.master_data_aliases (
    id TEXT PRIMARY KEY CHECK (id ~ '^mdali_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    project_id TEXT REFERENCES app.projects(id) ON DELETE RESTRICT,
    node_id TEXT NOT NULL,

    -- Which namespace this value lives in (a connector, a locale catalogue, a
    -- client spreadsheet). Opaque to the core.
    namespace TEXT NOT NULL CHECK (length(btrim(namespace)) BETWEEN 1 AND 80),
    locale TEXT CHECK (locale IS NULL OR locale ~ '^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$'),
    raw_value TEXT NOT NULL CHECK (length(btrim(raw_value)) BETWEEN 1 AND 400),
    -- Case-folded and whitespace-collapsed by the service. Stored so a
    -- collision is a database fact, not a query-time coincidence.
    normalized_value TEXT NOT NULL CHECK (length(btrim(normalized_value)) BETWEEN 1 AND 400),

    relation TEXT NOT NULL
        CHECK (relation IN ('exact', 'close', 'broader', 'narrower', 'related', 'negative')),
    confidence NUMERIC(5, 4) CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),

    effective_from DATE NOT NULL DEFAULT DATE '0001-01-01',
    effective_to DATE CHECK (effective_to IS NULL OR effective_to > effective_from),

    provenance TEXT NOT NULL DEFAULT 'operator'
        CHECK (provenance IN ('operator', 'connector', 'preset', 'inbound_match', 'import')),
    provenance_reference TEXT,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) = 'object'),

    -- A contradiction is RECORDED, never resolved by write order. The server
    -- refusing to pick is the whole point (AC5).
    conflict_state TEXT NOT NULL DEFAULT 'none'
        CHECK (conflict_state IN ('none', 'collision', 'contradiction')),

    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    retired_at TIMESTAMPTZ,
    retired_by TEXT,

    FOREIGN KEY (project_id, node_id)
        REFERENCES app.master_data_nodes(project_id, id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_master_data_aliases_node
    ON app.master_data_aliases (node_id) WHERE retired_at IS NULL;

-- Two LIVE exact aliases for the same normalized value in the same namespace
-- would mean one string names two objects. That is a collision the service must
-- surface, so the database refuses to let it exist silently.
CREATE UNIQUE INDEX IF NOT EXISTS uq_master_data_aliases_live_exact
    ON app.master_data_aliases (org_id, namespace, normalized_value, COALESCE(locale, ''))
    WHERE relation = 'exact' AND retired_at IS NULL AND effective_to IS NULL;

COMMENT ON TABLE app.master_data_aliases IS
    'Story 49.2: SKOS-typed mappings. `relation` is mandatory because exact, related and negative are three different claims and an untyped alias makes all three look alike.';

-- ---------------------------------------------------------------------------
-- 5. Project associations. An organization object is REUSED, with a Project
--    role and applicability. Copying it into a Project master is the duplicate
--    authority this story exists to remove.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.master_data_project_associations (
    id TEXT PRIMARY KEY CHECK (id ~ '^mdass_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    project_id TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    node_id TEXT NOT NULL,

    -- The pinned organization version this Project is associated with. NULL
    -- means "whatever is current" -- a legitimate choice, recorded explicitly
    -- rather than inferred from a missing column.
    node_version_id TEXT REFERENCES app.master_data_object_versions(id) ON DELETE RESTRICT,

    -- Opaque to the core (AD-2): 'own_brand' and 'competitor' are Competitor's
    -- roles, and this table does not know them.
    project_role TEXT NOT NULL CHECK (project_role ~ '^[a-z][a-z0-9_]{1,39}$'),
    applicability JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(applicability) = 'object'),

    effective_from DATE NOT NULL DEFAULT DATE '0001-01-01',
    effective_to DATE CHECK (effective_to IS NULL OR effective_to > effective_from),

    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    retired_at TIMESTAMPTZ,
    retired_by TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_master_data_association_live
    ON app.master_data_project_associations (project_id, node_id, project_role)
    WHERE retired_at IS NULL AND effective_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_master_data_association_node
    ON app.master_data_project_associations (node_id) WHERE retired_at IS NULL;

COMMENT ON TABLE app.master_data_project_associations IS
    'Story 49.2: an organization object reused by a Project with a role. The alternative -- copying the object -- is the second authority this story removes.';

-- ---------------------------------------------------------------------------
-- 6. Preserved identities. Business Domains already have stable IDs (Story
--    45.1) and consumers pinning them. Converging on one authority must not
--    mint new ones, so the node id pattern accepts a preserved id alongside a
--    freshly minted one.
--
--    The rows themselves are NOT moved here. A backfill that guesses which
--    classification is a Product and which is an Activity is precisely what
--    the Implementation Gate forbids ("never guess"), and it needs an operator
--    reading its own taxonomy. The service supports the convergence; the data
--    moves under a governed, audited command.
-- ---------------------------------------------------------------------------
ALTER TABLE app.master_data_nodes
    DROP CONSTRAINT IF EXISTS master_data_nodes_id_check;
ALTER TABLE app.master_data_nodes
    ADD CONSTRAINT master_data_nodes_id_check
    CHECK (id ~ '^(mdnode|bd|bcl)_[0-9A-HJKMNP-TV-Z]{26}$');

COMMENT ON COLUMN app.master_data_nodes.id IS
    'Story 49.2: a minted mdnode_ id, or a preserved bd_/bcl_ id carried over from Story 45.1 so that every consumer pinning it keeps resolving.';

COMMIT;
