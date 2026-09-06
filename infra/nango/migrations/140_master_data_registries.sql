-- Story 48.2: the generic Master Data owner Country needs, and does not own.
--
-- Story 48.1 gave every capability an exact per-Datastream proposal, and every
-- proposal a Governance owner reference. For Country that reference pointed at
-- `registry/project-geography` -- an identity minted from a content hash,
-- because no Governance object existed to carry one. Two authorities lived
-- outside any versioned owner and are retired here:
--
--   app.project_preferences.local_markets     mutable JSON, no version, no
--                                             Region, no effective membership
--   app.market_bindings                       mutable used-by, whose read
--                                             failure returned an EMPTY set --
--                                             an outage that reads as "nothing
--                                             depends on this"
--
-- What lands is deliberately NOT geography. Story 48.2's own Implementation
-- Gate forbids a Country-specific Governance lifecycle, so every table below is
-- keyed by `object_kind` and carries no country column. Country is the first
-- registry mounted on it (see core.country_registry); Competitors, Business
-- Domains and the rest of the Epic 49 object set mount the same five tables
-- without a second lifecycle.
--
-- Five objects, all Project-scoped except the two platform vocabularies:
--
--   app.master_data_registries          the stable owner: one per (project, kind)
--   app.master_data_nodes               stable identities whose LABEL may change
--   app.master_data_object_versions     immutable content once it leaves draft
--   app.master_data_memberships         hierarchy edges, bound to one version
--   app.master_data_used_by             version-bound dependents, fail-closed
--
-- plus, platform-scoped and immutable from birth:
--
--   app.master_data_vocabulary_versions the canonical value set (ISO for Country)
--   app.master_data_preset_versions     inert starting points, never a live dep

BEGIN;

-- ---------------------------------------------------------------------------
-- The registry. One stable owner per (Project, object kind) -- the identity a
-- capability proposal, an owner reference and a canonical route all address.
--
-- The three version pointers are distinct on purpose (AC3): a failed or
-- cancelled publication must leave `current_version_id` untouched, and
-- `last_known_good_version_id` is what a rollback reads. Collapsing them into
-- one column is exactly how a partial publication becomes invisible.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.master_data_registries (
    id TEXT PRIMARY KEY CHECK (id ~ '^mdreg_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    project_id TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,

    -- Opaque to this core (AD-2): the kind names the owner's domain, and this
    -- file never branches on its value. `country` is one of them, not the model.
    object_kind TEXT NOT NULL CHECK (object_kind ~ '^[a-z][a-z0-9_]{1,39}$'),
    label TEXT NOT NULL CHECK (length(btrim(label)) BETWEEN 1 AND 120),

    -- Draft, Pending and Active all appear in Master Data navigation; `disabled`
    -- does not (AC1: "a fully disabled capability does not pollute navigation").
    lifecycle_state TEXT NOT NULL DEFAULT 'draft'
        CHECK (lifecycle_state IN ('draft', 'pending', 'active', 'disabled')),

    current_version_id TEXT,
    pending_version_id TEXT,
    last_known_good_version_id TEXT,

    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (project_id, id),
    -- One owner per kind: a second registry for the same kind is a second
    -- authority, which is the defect this migration exists to remove.
    UNIQUE (project_id, object_kind)
);

CREATE INDEX IF NOT EXISTS idx_master_data_registries_project
    ON app.master_data_registries (project_id, lifecycle_state);

-- ---------------------------------------------------------------------------
-- The platform vocabulary. Immutable from birth: the canonical value set a
-- hierarchy version is pinned to.
--
-- For Country this is the assigned ISO 3166-1 alpha-2 set. `entries` records
-- each value's assignment status, so a user-assigned extension can be retained
-- and LABELLED without ever being presented as an official assignment (AC2).
-- The dbt seed becomes a generated projection of this table, not its source.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.master_data_vocabulary_versions (
    id TEXT PRIMARY KEY CHECK (id ~ '^mdvoc_[0-9A-HJKMNP-TV-Z]{26}$'),
    vocabulary_key TEXT NOT NULL CHECK (vocabulary_key ~ '^[a-z][a-z0-9_]{1,39}$'),

    -- Where the snapshot came from, and as of when. A build operation records
    -- these; no runtime call to ISO, UN or CLDR is ever made (AC2).
    source_authority TEXT NOT NULL CHECK (length(btrim(source_authority)) BETWEEN 1 AND 120),
    source_version TEXT NOT NULL CHECK (length(btrim(source_version)) BETWEEN 1 AND 80),
    source_reference TEXT,
    effective_date DATE NOT NULL,

    -- [{code, display_name, status, assignment, aliases[]}] -- `assignment` is
    -- 'officially_assigned' or 'user_assigned_extension'; the second is never
    -- rendered as the first.
    entries JSONB NOT NULL CHECK (jsonb_typeof(entries) = 'array'),
    entry_count INTEGER NOT NULL CHECK (entry_count > 0),
    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),

    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Deterministic: re-importing the same snapshot resolves to the same row
    -- instead of minting a rival vocabulary with identical content.
    UNIQUE (vocabulary_key, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_master_data_vocabulary_key
    ON app.master_data_vocabulary_versions (vocabulary_key, effective_date DESC);

CREATE OR REPLACE FUNCTION app.reject_master_data_vocabulary_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'master data vocabulary versions are immutable';
END;
$$;
DROP TRIGGER IF EXISTS trg_master_data_vocabulary_immutable
    ON app.master_data_vocabulary_versions;
CREATE TRIGGER trg_master_data_vocabulary_immutable
    BEFORE UPDATE OR DELETE ON app.master_data_vocabulary_versions
    FOR EACH ROW EXECUTE FUNCTION app.reject_master_data_vocabulary_mutation();

-- ---------------------------------------------------------------------------
-- The inert preset. Explicit members, stated provenance, no live dependency.
--
-- Story 37.9 banned presets outright because a preset that applies itself is an
-- authority nobody chose. The ban is replaced (AC4) by the property that made
-- it dangerous: a preset here is READ to build a draft and is never consulted
-- again. `classification` keeps a commercial grouping from being presented as a
-- standard: EMEA/APAC/AMER is a toorow starter, UN M49 is standard-derived.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.master_data_preset_versions (
    id TEXT PRIMARY KEY CHECK (id ~ '^mdpre_[0-9A-HJKMNP-TV-Z]{26}$'),
    object_kind TEXT NOT NULL CHECK (object_kind ~ '^[a-z][a-z0-9_]{1,39}$'),
    preset_key TEXT NOT NULL CHECK (preset_key ~ '^[a-z][a-z0-9_-]{1,63}$'),
    label TEXT NOT NULL CHECK (length(btrim(label)) BETWEEN 1 AND 120),
    description TEXT NOT NULL CHECK (length(btrim(description)) BETWEEN 1 AND 600),

    classification TEXT NOT NULL
        CHECK (classification IN ('standard_derived', 'toorow_curated')),
    source_authority TEXT NOT NULL CHECK (length(btrim(source_authority)) BETWEEN 1 AND 120),
    source_reference TEXT,
    preset_version TEXT NOT NULL CHECK (length(btrim(preset_version)) BETWEEN 1 AND 40),

    -- {nodes: [{key, node_kind, label, parent_key}],
    --  members: [{parent_key, value, optional, default_selected, note}]}
    -- `optional` is what keeps the France starter honest: FR is fixed, RE and
    -- GF are offered and never attached silently (AC4).
    payload JSONB NOT NULL CHECK (
        jsonb_typeof(payload) = 'object'
        AND payload ? 'nodes'
        AND payload ? 'members'
    ),
    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (object_kind, preset_key, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_master_data_presets_kind
    ON app.master_data_preset_versions (object_kind, preset_key);

CREATE OR REPLACE FUNCTION app.reject_master_data_preset_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'master data preset versions are immutable';
END;
$$;
DROP TRIGGER IF EXISTS trg_master_data_presets_immutable
    ON app.master_data_preset_versions;
CREATE TRIGGER trg_master_data_presets_immutable
    BEFORE UPDATE OR DELETE ON app.master_data_preset_versions
    FOR EACH ROW EXECUTE FUNCTION app.reject_master_data_preset_mutation();

-- ---------------------------------------------------------------------------
-- The stable node. A Market keeps its identity across a rename, a move and a
-- membership change -- that is the whole difference between this and the JSON
-- list it replaces, where renaming a market silently broke every binding to it.
--
-- The label lives here (mutable); WHERE the node sits and WHAT it contains live
-- in the immutable version. `archived_at` retires a node without deleting the
-- identity older versions still reference.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.master_data_nodes (
    id TEXT PRIMARY KEY CHECK (id ~ '^mdnode_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    project_id TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    registry_id TEXT NOT NULL,

    -- Country's kinds are 'market', 'region' and 'rest_of_world'. The core does
    -- not know them; it only enforces that a kind is named.
    node_kind TEXT NOT NULL CHECK (node_kind ~ '^[a-z][a-z0-9_]{1,39}$'),
    label TEXT NOT NULL CHECK (length(btrim(label)) BETWEEN 1 AND 120),

    -- Provenance only. A materialized draft records which preset it came from
    -- and then owes that preset nothing (AC4).
    origin_preset_version_id TEXT REFERENCES app.master_data_preset_versions(id)
        ON DELETE RESTRICT,

    archived_at TIMESTAMPTZ,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (project_id, id),
    FOREIGN KEY (project_id, registry_id)
        REFERENCES app.master_data_registries(project_id, id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_master_data_nodes_registry
    ON app.master_data_nodes (project_id, registry_id, node_kind);

-- Rest of World is a singleton per registry: two catch-alls would make the
-- additive reconciliation of AC5 unprovable. Archived ones do not count.
CREATE UNIQUE INDEX IF NOT EXISTS uq_master_data_nodes_singleton_catch_all
    ON app.master_data_nodes (project_id, registry_id)
    WHERE node_kind = 'rest_of_world' AND archived_at IS NULL;

-- ---------------------------------------------------------------------------
-- The version. Editable while `draft`; frozen the moment it becomes anything
-- else. Draft, candidate, current and last-known-good stay distinct (AC3).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.master_data_object_versions (
    id TEXT PRIMARY KEY CHECK (id ~ '^mdver_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    project_id TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    registry_id TEXT NOT NULL,

    version_number INTEGER NOT NULL CHECK (version_number > 0),
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'candidate', 'current', 'superseded')),

    vocabulary_version_id TEXT NOT NULL
        REFERENCES app.master_data_vocabulary_versions(id) ON DELETE RESTRICT,

    -- Everything the version means beyond its edges: the Rest of World label,
    -- parent and default drill policy, and the alias/conformance version this
    -- content was reviewed against.
    payload JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(payload) = 'object'),

    origin_preset_version_id TEXT REFERENCES app.master_data_preset_versions(id)
        ON DELETE RESTRICT,

    -- Frozen at publication so a later outage cannot retroactively empty it.
    used_by_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(used_by_snapshot) = 'array'),
    alias_version_fingerprint TEXT
        CHECK (alias_version_fingerprint IS NULL OR alias_version_fingerprint ~ '^[0-9a-f]{64}$'),

    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    effective_date DATE,

    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at TIMESTAMPTZ,
    published_by TEXT,

    UNIQUE (project_id, id),
    UNIQUE (project_id, registry_id, version_number),
    -- A published version always says when and by whom.
    CHECK ((status IN ('current', 'superseded')) = (published_at IS NOT NULL)),
    FOREIGN KEY (project_id, registry_id)
        REFERENCES app.master_data_registries(project_id, id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_master_data_versions_registry
    ON app.master_data_object_versions (project_id, registry_id, status, version_number DESC);

-- At most one `current` version per registry. Publication supersedes the
-- previous one in the same transaction, so two current versions never coexist.
CREATE UNIQUE INDEX IF NOT EXISTS uq_master_data_versions_single_current
    ON app.master_data_object_versions (project_id, registry_id)
    WHERE status = 'current';

-- Content is immutable once the version leaves draft, and a published version
-- may only ever be superseded. Nothing is deleted: an old Result pins a version
-- id and must stay reproducible (AC7).
CREATE OR REPLACE FUNCTION app.protect_master_data_object_version()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION 'published master data versions are never deleted';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status <> 'draft' AND (
           NEW.payload IS DISTINCT FROM OLD.payload
        OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
        OR NEW.vocabulary_version_id IS DISTINCT FROM OLD.vocabulary_version_id
        OR NEW.used_by_snapshot IS DISTINCT FROM OLD.used_by_snapshot
        OR NEW.version_number IS DISTINCT FROM OLD.version_number
        OR NEW.registry_id IS DISTINCT FROM OLD.registry_id
        OR NEW.project_id IS DISTINCT FROM OLD.project_id
    ) THEN
        RAISE EXCEPTION 'master data version content is immutable once it leaves draft';
    END IF;
    IF OLD.status = 'superseded' AND NEW.status <> 'superseded' THEN
        RAISE EXCEPTION 'a superseded master data version never returns to service';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_master_data_versions_protect
    ON app.master_data_object_versions;
CREATE TRIGGER trg_master_data_versions_protect
    BEFORE UPDATE OR DELETE ON app.master_data_object_versions
    FOR EACH ROW EXECUTE FUNCTION app.protect_master_data_object_version();

-- The registry's three pointers must name versions of that same registry.
ALTER TABLE app.master_data_registries
    DROP CONSTRAINT IF EXISTS fk_master_data_registries_current;
ALTER TABLE app.master_data_registries
    ADD CONSTRAINT fk_master_data_registries_current
    FOREIGN KEY (project_id, current_version_id)
    REFERENCES app.master_data_object_versions(project_id, id) ON DELETE RESTRICT;

ALTER TABLE app.master_data_registries
    DROP CONSTRAINT IF EXISTS fk_master_data_registries_pending;
ALTER TABLE app.master_data_registries
    ADD CONSTRAINT fk_master_data_registries_pending
    FOREIGN KEY (project_id, pending_version_id)
    REFERENCES app.master_data_object_versions(project_id, id) ON DELETE RESTRICT;

ALTER TABLE app.master_data_registries
    DROP CONSTRAINT IF EXISTS fk_master_data_registries_lkg;
ALTER TABLE app.master_data_registries
    ADD CONSTRAINT fk_master_data_registries_lkg
    FOREIGN KEY (project_id, last_known_good_version_id)
    REFERENCES app.master_data_object_versions(project_id, id) ON DELETE RESTRICT;

-- ---------------------------------------------------------------------------
-- The hierarchy edge. Membership belongs to a version, never to a node: that is
-- what makes a regroup a new VERSION rather than a mutation nobody can date.
--
-- A child is either another node (Market inside Region) or a canonical
-- vocabulary value (a country code inside a Market). Exactly one, never both.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.master_data_memberships (
    id TEXT PRIMARY KEY CHECK (id ~ '^mdmem_[0-9A-HJKMNP-TV-Z]{26}$'),
    project_id TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    version_id TEXT NOT NULL,
    parent_node_id TEXT NOT NULL,

    child_node_id TEXT,
    child_value TEXT CHECK (child_value IS NULL OR length(btrim(child_value)) BETWEEN 1 AND 64),

    effective_from DATE NOT NULL DEFAULT DATE '0001-01-01',
    effective_to DATE,
    display_order INTEGER NOT NULL DEFAULT 0,

    CHECK ((child_node_id IS NULL) <> (child_value IS NULL)),
    CHECK (effective_to IS NULL OR effective_to > effective_from),
    -- A node containing itself is the shortest cycle; the service rejects the
    -- longer ones, and this rejects the one a single statement can create.
    CHECK (child_node_id IS NULL OR child_node_id <> parent_node_id),

    FOREIGN KEY (project_id, version_id)
        REFERENCES app.master_data_object_versions(project_id, id) ON DELETE CASCADE,
    FOREIGN KEY (project_id, parent_node_id)
        REFERENCES app.master_data_nodes(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, child_node_id)
        REFERENCES app.master_data_nodes(project_id, id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_master_data_memberships_version
    ON app.master_data_memberships (project_id, version_id, parent_node_id, display_order);
CREATE INDEX IF NOT EXISTS idx_master_data_memberships_child_value
    ON app.master_data_memberships (project_id, version_id, child_value)
    WHERE child_value IS NOT NULL;

-- One effective home per value and per node, inside one version. The open-ended
-- case is the one every editor produces, so it is enforced here; dated overlaps
-- are rejected by the service, which can see the ranges this index cannot.
CREATE UNIQUE INDEX IF NOT EXISTS uq_master_data_membership_open_value
    ON app.master_data_memberships (project_id, version_id, child_value)
    WHERE child_value IS NOT NULL AND effective_to IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_master_data_membership_open_node
    ON app.master_data_memberships (project_id, version_id, child_node_id)
    WHERE child_node_id IS NOT NULL AND effective_to IS NULL;

-- Edges of a frozen version are frozen with it.
CREATE OR REPLACE FUNCTION app.protect_master_data_membership()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    parent_status TEXT;
    target_version TEXT;
    target_project TEXT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        target_version := OLD.version_id;
        target_project := OLD.project_id;
    ELSE
        target_version := NEW.version_id;
        target_project := NEW.project_id;
    END IF;
    SELECT status INTO parent_status
        FROM app.master_data_object_versions
        WHERE project_id = target_project AND id = target_version;
    -- A cascading delete of a draft version removes its rows before this fires
    -- with no version left to read; that is the one legal absence.
    IF parent_status IS NOT NULL AND parent_status <> 'draft' THEN
        RAISE EXCEPTION 'membership of a published master data version is immutable';
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_master_data_memberships_protect
    ON app.master_data_memberships;
CREATE TRIGGER trg_master_data_memberships_protect
    BEFORE INSERT OR UPDATE OR DELETE ON app.master_data_memberships
    FOR EACH ROW EXECUTE FUNCTION app.protect_master_data_membership();

-- ---------------------------------------------------------------------------
-- Used-by. What depends on a node, and at which version it was recorded.
--
-- app.market_bindings, which this replaces, was mutable and unversioned, and
-- its reader returned an empty tuple when the read failed -- so a database
-- outage looked exactly like "no dependents" and let a destructive membership
-- change through. Here the reader raises, and the caller fails closed (AC8).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.master_data_used_by (
    id TEXT PRIMARY KEY CHECK (id ~ '^mduse_[0-9A-HJKMNP-TV-Z]{26}$'),
    project_id TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    registry_id TEXT NOT NULL,
    node_id TEXT NOT NULL,

    -- Caller data (AD-2): enumerating consumers here would make the platform
    -- decide which surfaces are allowed to depend on Master Data.
    consumer_kind TEXT NOT NULL CHECK (length(btrim(consumer_kind)) BETWEEN 1 AND 60),
    consumer_id TEXT NOT NULL CHECK (length(btrim(consumer_id)) BETWEEN 1 AND 200),
    consumer_label TEXT,
    consumer_version_id TEXT,
    hierarchy_version_id TEXT,

    created_by TEXT NOT NULL DEFAULT 'system',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    released_at TIMESTAMPTZ,

    FOREIGN KEY (project_id, registry_id)
        REFERENCES app.master_data_registries(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, node_id)
        REFERENCES app.master_data_nodes(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, hierarchy_version_id)
        REFERENCES app.master_data_object_versions(project_id, id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_master_data_used_by_identity
    ON app.master_data_used_by
       (project_id, node_id, consumer_kind, consumer_id, COALESCE(consumer_version_id, ''));
CREATE INDEX IF NOT EXISTS idx_master_data_used_by_node
    ON app.master_data_used_by (project_id, node_id)
    WHERE released_at IS NULL;

COMMENT ON TABLE app.master_data_registries IS
    'Story 48.2: the generic Master Data owner. One registry per (project, object_kind); Country is the first kind mounted on it, not its model.';
COMMENT ON TABLE app.master_data_used_by IS
    'Story 48.2: version-bound dependents. Replaces the mutable app.market_bindings, whose read failure returned an empty set and could bypass a used-by guard.';

COMMIT;
