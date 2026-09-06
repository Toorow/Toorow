-- Story 68.6: a derived attribute names the rule version that made it.
--
-- WHAT THIS TABLE IS. A rule set mounted on the generic governance lifecycle
-- (Story 48.3/49.4, family `entity_derivation`) derives classifications for
-- the nodes of a declared entity type (68.1) -- `content_type` from a
-- duration, a tier from a size. Publishing a version COMPUTES those
-- attributes for the registry's nodes and writes them here, every row stamped
-- with the rule-set version id that produced it (AC2): a reader can always
-- name which version produced a value.
--
-- WHY A TABLE OF ITS OWN, AND NOT THE NODE PAYLOAD. The facts a rule reads
-- live in `master_data_object_versions.payload.attributes` (the surface
-- migration 241 projects). Writing the derived value THERE would rewrite the
-- node version -- the fact -- and CAP-4 forbids exactly that: a
-- reclassification must re-derive answers without mutating facts. So the
-- derived value is a row of its own, keyed by (node, attribute, rule
-- version). Publishing a NEW version writes NEW rows; the old ones stay,
-- because an old Result may pin them; the read path resolves the current
-- published version at read time and picks its rows (AC3). Nothing here is
-- ever updated or deleted.
--
-- WHY `value` IS NOT NULL. A node no rule matches (and whose attribute
-- declares no `otherwise`) derives NOTHING, and no row is written -- the
-- absence of a row IS "unclassified under this version", and a stored NULL
-- would make that indistinguishable from a real JSON null value.
--
-- THE FOREIGN KEYS ARE THE GOVERNANCE. The composite key on
-- (rule_set_version_id, rule_set_id, project_id) reads
-- `uq_governance_rule_set_version_scope`, so a stamped row cannot name a
-- version of another Project -- the same argument migration 236 makes for a
-- pinned mapping. ON DELETE RESTRICT everywhere: a version that produced
-- classifications cannot disappear from under them.
--
-- RLS: the table is project-scoped with `org_id` NOT NULL, so it takes the
-- 273 first-form predicate, verbatim -- the ratchet
-- `test_rls_covers_every_org_scoped_table_pg.py` walks information_schema and
-- would refuse the table otherwise.

BEGIN;

CREATE TABLE IF NOT EXISTS app.master_data_derived_attributes (
    id TEXT PRIMARY KEY CHECK (id ~ '^mdder_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    project_id TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,

    -- Whose value this is. The kind itself stays on the registry; this table
    -- never repeats it (AD-2: an opaque string the core never compares).
    registry_id TEXT NOT NULL,
    node_id TEXT NOT NULL,

    attribute TEXT NOT NULL CHECK (attribute ~ '^[a-z][a-z0-9_]{0,63}$'),
    value JSONB NOT NULL,

    -- The stamp. The whole point of the table: which rule-set version
    -- produced this value (AC2). Never "the latest" -- the exact version.
    rule_set_id TEXT NOT NULL,
    rule_set_version_id TEXT NOT NULL,

    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    FOREIGN KEY (project_id, registry_id)
        REFERENCES app.master_data_registries(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, node_id)
        REFERENCES app.master_data_nodes(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (rule_set_version_id, rule_set_id, project_id)
        REFERENCES app.governance_rule_set_versions(id, rule_set_id, project_id)
        ON DELETE RESTRICT
);

-- One value per (node, attribute) PER VERSION: the index is what makes two
-- versions of the same rule set coexist, and a replayed publish a no-op.
CREATE UNIQUE INDEX IF NOT EXISTS uq_master_data_derived_attributes_version
    ON app.master_data_derived_attributes (project_id, node_id, attribute, rule_set_version_id);

-- The read path: every row the current published version of a type produced.
CREATE INDEX IF NOT EXISTS idx_master_data_derived_attributes_version_read
    ON app.master_data_derived_attributes (project_id, registry_id, rule_set_version_id);

COMMENT ON TABLE app.master_data_derived_attributes IS
    'Story 68.6: entity attributes DERIVED by a published governance rule-set version, one row per (node, attribute, version). Facts (node payloads) are never rewritten -- a new version writes new stamped rows and the read path resolves the current version at read time (CAP-4).';

COMMENT ON COLUMN app.master_data_derived_attributes.rule_set_version_id IS
    'The stamp (AC2): the exact rule-set version whose evaluation produced this value. A reader can always name which version produced a value; a reclassification is a new version, never an update.';

ALTER TABLE app.master_data_derived_attributes ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.master_data_derived_attributes FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS master_data_derived_attributes_epic36 ON app.master_data_derived_attributes;
CREATE POLICY master_data_derived_attributes_epic36 ON app.master_data_derived_attributes
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

COMMIT;
