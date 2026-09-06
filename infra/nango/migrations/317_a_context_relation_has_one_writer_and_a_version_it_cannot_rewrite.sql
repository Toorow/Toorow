-- 317 -- a context relation gets ONE writer, and a version row that outlives
--        the relation itself.
--
-- MEASURED before a line, 2026-08-28:
--
--   grep -n "context_relationships" infra/nango/migrations/*.sql   -> 0
--   ls server/core/context_relationships.py                        -> absent
--   context_store.py:1771 create_graph_edge   -> a hard INSERT into app.context_graph
--   context_store.py:1861 delete_graph_edge   -> a hard DELETE from app.context_graph
--
-- So the only cross-owner relation the product had was an UN-VERSIONED row that
-- a DELETE destroyed. A knowledge topic could be linked to a governed field and
-- the link could vanish leaving no trace of who made it, when, or why -- while
-- every OTHER governed object of this repository has carried an append-only
-- version since migration 200. `app.context_graph` also carries no `org_id`, no
-- RLS and no foreign key at all (031:41-56): a relation was, literally, the one
-- governed fact nobody governed.
--
-- WHAT THIS ADDS
--
--   app.context_relationships          -- the head: identity, scope, endpoints,
--                                         kind, status, provenance, actor
--   app.context_relationship_versions  -- append-only: one row per act, with the
--                                         typed fact and its content hash
--
-- and it turns `app.context_graph` into a READ PROJECTION written by the
-- authority in the SAME transaction. That is not an invention: it is character
-- for character the shape `governance.md` Decision 2 ratified on 2026-08-25 for
-- the superseded taxonomy store -- *"the projection stays: a creation in the
-- authority writes, in the same transaction, one legacy row [...] the readers
-- keep working, and they are re-pointed reader by reader, never by a
-- big-bang."* Four non-test readers resolve a relation through
-- `app.context_graph` today (`context_search.py:1098`, `context_seed.py:437`,
-- `datamodel.py:1551`, `mirror_sync.py:471`) and none of them is touched.
--
-- ============================================================================
-- THE SCOPE IS NULLABLE, AND THAT IS THE INCUMBENT'S SHAPE, NOT A WEAKENING
-- ============================================================================
--
-- `app.context_graph.project_id` is NULLABLE and NULL means the PLATFORM scope:
-- a relation between two platform topics, visible from every project, written
-- only by a caller `context_api.check_platform_write_authorized` lets through.
-- The authority carries the same two scopes or it cannot carry the incumbent
-- rows. `org_id` is derived from the project graph and is therefore NULL for
-- exactly the same rows -- `ck_context_relationships_scope_is_whole` refuses
-- every half-stated pair, and it is NULL-proof (both sides of the `=` are
-- `IS NULL` predicates, never a nullable value).
--
-- ============================================================================
-- `superseded_by` IS DERIVED, AND THAT IS WHY IT IS NOT A COLUMN
-- ============================================================================
--
-- Story 49-6 AC1 asks the version table for a *"supersession fact with
-- `superseded_by`"*. A column of that name cannot exist on an append-only
-- table: filling it on version N the day version N+1 is written is an UPDATE,
-- and the UPDATE is exactly what the trigger below refuses. So the fact is
-- stored on the SUCCESSOR -- `supersedes_version_id` -- and `superseded_by` is
-- READ back by the successor's own row (`context_relationships.read_relationship`
-- computes it). CLAUDE.md: *une valeur derivable ne se stocke pas*. Nothing is
-- lost: the chain is complete in both directions, and only one of the two ends
-- is writable, which is the property that makes it evidence.
--
-- ============================================================================
-- THE BACKFILL FAILS LOUDLY. IT NEVER MERGES, AND IT NEVER DROPS.
-- ============================================================================
--
-- The story offers two policies and asks that one be named here with its
-- reason. THIS MIGRATION FAILS LOUDLY, and writes no conflicts table, because
-- the two conflict classes a conflicts table would hold are BOTH classes a
-- person has to arbitrate -- and because the third class, the duplicate, cannot
-- occur:
--
--  * DUPLICATES ARE ALREADY IMPOSSIBLE. `uq_context_graph_edge` (031:56) is
--    UNIQUE on (from_id, from_type, to_id, to_type, edge_type) with NO
--    project_id in the key. The incumbent store therefore already refuses two
--    edges that would collapse onto one relation, and a conflicts table for the
--    duplicate class would be a table that can never hold a row.
--
--  * A MASTER DATA <-> MASTER DATA EDGE IS 49.2'S, NOT OURS. Migration 272 let
--    both endpoints of an edge be a `master_data_node`, and story 49-6 AC2
--    refuses that relation here by name -- it belongs to
--    `app.mdm_business_links` and the 49.2 service. Carrying such a row would
--    mint a SECOND authority for one fact; dropping it would be the silent
--    merge the story's *Incomplete if* forbids. Neither is a migration's
--    decision, so the migration stops and names the count.
--
--  * AN EDGE NAMING A PROJECT THAT NO LONGER EXISTS cannot be carried either:
--    `app.context_graph` has no foreign key, the authority does, and inventing
--    an org for an orphan is fabricating a scope. It stops and names the count.
--
-- PRE-FLIGHT -- run these two BEFORE applying 317 to a populated database; each
-- returns 0 on a database this migration can carry:
--
--   SELECT count(*) FROM app.context_graph
--    WHERE from_type = 'master_data_node' AND to_type = 'master_data_node';
--
--   SELECT count(*) FROM app.context_graph e
--    WHERE e.project_id IS NOT NULL
--      AND NOT EXISTS (SELECT 1 FROM app.projects p WHERE p.id = e.project_id);
--
-- ============================================================================
-- THE KIND IS CARRIED, NEVER INTERPRETED
-- ============================================================================
--
-- `relationship_kind` carries a length CHECK and no enumeration. The vocabulary
-- (`context_store.GRAPH_EDGE_TYPES`) is enforced by the SERVICE, at the only
-- door that mints a NEW relation. An incumbent edge predating that vocabulary
-- -- and `context_store.py:1569` records that free-form strings made the graph
-- "a pile of vocabularies" before it existed -- is carried VERBATIM rather than
-- refused by a constraint name. This is the posture `governance.md` states for
-- `classification_type`: what is carried is never interpreted, and the
-- interpretation is a human act taken later, one row at a time.
--
-- ============================================================================
-- ERASURE, AND THE PRIVILEGE HALF OF IT
-- ============================================================================
--
-- Both tables hang off `app.projects (org_id, id)` ON DELETE CASCADE, so
-- `core.org_purge` reaches them through the graph it walks. The append-only
-- trigger on the versions carries the `app.rgpd_erasure` WHEN clause of
-- migrations 099/200/209 -- an append-only table that forgets it re-blocks an
-- organization erasure, and this repository has already repaired that twice.
--
-- The GRANTs are written WITH their REVOKE, which is migration 316's lesson in
-- one line: 207's `ALTER DEFAULT PRIVILEGES` hands connector SELECT, INSERT,
-- UPDATE, DELETE on every table created after it, so `GRANT SELECT, INSERT` is
-- a COMMENT until a REVOKE states the other half. DELETE is granted on both --
-- the erasure needs it, and `test_the_erasure_hatch_is_a_privilege_too.py`
-- checks that it is there.

BEGIN;

-- ---------------------------------------------------------------------------
-- 0. The two classes this migration refuses to decide on behalf of a person.
-- ---------------------------------------------------------------------------

DO $preflight$
DECLARE
    mdm_internal BIGINT;
    orphaned     BIGINT;
BEGIN
    IF to_regclass('app.context_graph') IS NULL THEN
        RAISE EXCEPTION
            'app.context_graph is missing -- apply migration 031 first (317 '
            'carries its rows into the relationship authority)';
    END IF;

    SELECT count(*) INTO mdm_internal
      FROM app.context_graph
     WHERE from_type = 'master_data_node' AND to_type = 'master_data_node';

    IF mdm_internal > 0 THEN
        RAISE EXCEPTION
            '317: % context_graph edge(s) relate one Master Data node to '
            'another. That relation belongs to the Master Data authority '
            '(app.mdm_business_links, story 49.2), not to the Context Hub. '
            'Retire each edge from the Knowledge Graph and declare the '
            'relation in Master Data, then apply 317 again.', mdm_internal;
    END IF;

    SELECT count(*) INTO orphaned
      FROM app.context_graph e
     WHERE e.project_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM app.projects p WHERE p.id = e.project_id);

    IF orphaned > 0 THEN
        RAISE EXCEPTION
            '317: % context_graph edge(s) name a project that no longer '
            'exists. The relationship authority derives the organization from '
            'the project graph and cannot invent one. Delete those edges from '
            'the Knowledge Graph, then apply 317 again.', orphaned;
    END IF;
END
$preflight$;

-- ---------------------------------------------------------------------------
-- 1. The head.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.context_relationships (
    id                  TEXT        NOT NULL,
    -- NULL/NULL is the platform scope, and the CHECK below refuses every half.
    org_id              TEXT,
    project_id          TEXT,
    source_type         TEXT        NOT NULL,
    source_id           TEXT        NOT NULL,
    target_type         TEXT        NOT NULL,
    target_id           TEXT        NOT NULL,
    relationship_kind   TEXT        NOT NULL,
    status              TEXT        NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'superseded')),
    provenance          TEXT        NOT NULL,
    -- The `app.context_graph` row this relation projects, or NULL when the
    -- relation names an endpoint the legacy store cannot express (a Semantic
    -- View, a metric, a Datastream). A projection nobody can hold is not
    -- written; the authority still holds the relation.
    projection_edge_id  TEXT,
    current_version_id  TEXT,
    created_by          TEXT        NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_context_relationships PRIMARY KEY (id),
    -- House id: Crockford base32 behind a prefix that says what the object is.
    CONSTRAINT ck_context_relationships_id
        CHECK (id ~ '^crel_[0-9A-HJKMNP-TV-Z]{26}$'),
    -- Both stated, or neither: an org without a project (or the reverse) is a
    -- scope no reader can apply. NULL-proof by construction.
    CONSTRAINT ck_context_relationships_scope_is_whole
        CHECK ((org_id IS NULL) = (project_id IS NULL)),
    CONSTRAINT ck_context_relationships_source_type
        CHECK (source_type IN ('topic', 'procedure', 'schema_doc', 'target_field',
                               'master_data_node', 'semantic_view', 'metric',
                               'datastream')),
    CONSTRAINT ck_context_relationships_target_type
        CHECK (target_type IN ('topic', 'procedure', 'schema_doc', 'target_field',
                               'master_data_node', 'semantic_view', 'metric',
                               'datastream')),
    -- Story 49-6 AC2, at the schema: a relation between two Master Data objects
    -- is the 49.2 service's, and no door of this store may hold one.
    CONSTRAINT ck_context_relationships_not_mdm_internal
        CHECK (NOT (source_type = 'master_data_node'
                    AND target_type = 'master_data_node')),
    CONSTRAINT ck_context_relationships_kind
        CHECK (length(btrim(relationship_kind)) BETWEEN 1 AND 60),
    CONSTRAINT ck_context_relationships_endpoints_named
        CHECK (length(btrim(source_id)) > 0 AND length(btrim(target_id)) > 0),
    -- The COUPLE, never project_id alone: it stops a relation from carrying
    -- another org's id and crossing the RLS by its own column. MATCH SIMPLE
    -- leaves the platform rows (both NULL) unconstrained, which is what the
    -- platform scope means.
    CONSTRAINT fk_context_relationships_project
        FOREIGN KEY (org_id, project_id) REFERENCES app.projects (org_id, id)
        ON DELETE CASCADE
);

-- One ACTIVE relation per (scope, source, target, kind). A superseded one frees
-- the tuple, so a retired relation can be re-declared -- and `restore` refuses
-- by name when a fresh one has taken the place.
CREATE UNIQUE INDEX IF NOT EXISTS uq_context_relationships_active
    ON app.context_relationships
       (COALESCE(project_id, ''), source_type, source_id,
        target_type, target_id, relationship_kind)
    WHERE status = 'active';

-- The two directions of the reverse-link facet (AC6), each answered by an index
-- rather than by a scan of the whole store.
CREATE INDEX IF NOT EXISTS idx_context_relationships_source
    ON app.context_relationships (source_type, source_id, status);
CREATE INDEX IF NOT EXISTS idx_context_relationships_target
    ON app.context_relationships (target_type, target_id, status);
CREATE INDEX IF NOT EXISTS idx_context_relationships_scope
    ON app.context_relationships (project_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_context_relationships_projection
    ON app.context_relationships (projection_edge_id)
    WHERE projection_edge_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 2. The versions -- append-only, and the only place a retirement is proven.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.context_relationship_versions (
    id                    TEXT        NOT NULL,
    relationship_id       TEXT        NOT NULL,
    org_id                TEXT,
    project_id            TEXT,
    version_number        INTEGER     NOT NULL CHECK (version_number >= 1),
    -- What the act was. `created` mints, `superseded` retires, `restored` puts
    -- back -- and every one of the three is a ROW, never a rewrite.
    lifecycle             TEXT        NOT NULL
                          CHECK (lifecycle IN ('created', 'superseded', 'restored')),
    -- The typed fact: {source: {type, id}, target: {type, id}, kind,
    -- provenance, reason}. IDS AND TYPED FACTS ONLY -- never a copied label,
    -- never a URL. The owner keeps its own payload; a relation names it.
    fact                  JSONB       NOT NULL
                          CHECK (jsonb_typeof(fact) = 'object'),
    -- The version this row replaces. `superseded_by` is its mirror and is READ
    -- from here, never written back onto the predecessor -- see the header.
    supersedes_version_id TEXT,
    -- Optional endpoint pins: the version of the source / of the target this
    -- relation was declared against, when the owner has versions to pin.
    source_version_id     TEXT,
    target_version_id     TEXT,
    content_hash          TEXT        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    created_by            TEXT        NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_context_relationship_versions PRIMARY KEY (id),
    CONSTRAINT ck_context_relationship_versions_id
        CHECK (id ~ '^crelv_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT ck_context_relationship_versions_scope_is_whole
        CHECK ((org_id IS NULL) = (project_id IS NULL)),
    CONSTRAINT uq_context_relationship_versions_number
        UNIQUE (relationship_id, version_number),
    CONSTRAINT fk_context_relationship_versions_relation
        FOREIGN KEY (relationship_id) REFERENCES app.context_relationships (id)
        ON DELETE CASCADE,
    CONSTRAINT fk_context_relationship_versions_predecessor
        FOREIGN KEY (supersedes_version_id)
        REFERENCES app.context_relationship_versions (id)
);

CREATE INDEX IF NOT EXISTS idx_context_relationship_versions_relation
    ON app.context_relationship_versions
       (relationship_id, version_number DESC);
CREATE INDEX IF NOT EXISTS idx_context_relationship_versions_scope
    ON app.context_relationship_versions (project_id);

-- The head pointer can only name a version of THIS relation. Without the
-- couple, a crossed pointer would read another relation's fact under this
-- one's identity. The UNIQUE index comes BEFORE the constraint: a foreign key
-- needs a unique index on the referenced columns, and the reverse order is
-- 42830 InvalidForeignKey (migration 315 learned this the hard way).
CREATE UNIQUE INDEX IF NOT EXISTS uq_context_relationship_versions_owner
    ON app.context_relationship_versions (id, relationship_id);

ALTER TABLE app.context_relationships
    DROP CONSTRAINT IF EXISTS fk_context_relationships_current_version;
ALTER TABLE app.context_relationships
    ADD CONSTRAINT fk_context_relationships_current_version
    FOREIGN KEY (current_version_id, id)
    REFERENCES app.context_relationship_versions (id, relationship_id)
    DEFERRABLE INITIALLY DEFERRED;

-- ---------------------------------------------------------------------------
-- 3. Append-only, with the erasure hatch. Migration 099's WHEN clause, copied
--    because it is the clause an org erasure needs, not because it is a habit.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION app.reject_context_relationship_version_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'context relationship versions are immutable'
        USING ERRCODE = '23000';
END;
$$;

DROP TRIGGER IF EXISTS trg_context_relationship_versions_immutable
    ON app.context_relationship_versions;
CREATE TRIGGER trg_context_relationship_versions_immutable
    BEFORE UPDATE OR DELETE ON app.context_relationship_versions
    FOR EACH ROW
    WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
    EXECUTE FUNCTION app.reject_context_relationship_version_mutation();

DROP TRIGGER IF EXISTS trg_context_relationship_versions_block_truncate
    ON app.context_relationship_versions;
CREATE TRIGGER trg_context_relationship_versions_block_truncate
    BEFORE TRUNCATE ON app.context_relationship_versions
    FOR EACH STATEMENT
    EXECUTE FUNCTION app.reject_context_relationship_version_mutation();

-- ---------------------------------------------------------------------------
-- 4. RLS on both. A platform row (project_id IS NULL) is readable from every
--    scope, exactly as a platform topic is; who may WRITE one is decided by
--    `context_api.check_platform_write_authorized`, deny-by-default.
-- ---------------------------------------------------------------------------

DO $rls$
DECLARE
    target  TEXT;
    guarded TEXT[] := ARRAY['context_relationships', 'context_relationship_versions'];
BEGIN
    FOREACH target IN ARRAY guarded LOOP
        EXECUTE format('ALTER TABLE app.%I ENABLE ROW LEVEL SECURITY', target);
        EXECUTE format('ALTER TABLE app.%I FORCE ROW LEVEL SECURITY', target);
        EXECUTE format('DROP POLICY IF EXISTS %I ON app.%I', target || '_epic36', target);
        EXECUTE format(
            'CREATE POLICY %I ON app.%I USING ('
            'current_setting(''toorow.enforce_epic36'', true) IS DISTINCT FROM ''on'' '
            'OR project_id IS NULL '
            'OR app.epic36_has_resource_access(org_id, ''project'', project_id)) '
            'WITH CHECK ('
            'current_setting(''toorow.enforce_epic36'', true) IS DISTINCT FROM ''on'' '
            'OR project_id IS NULL '
            'OR app.epic36_has_resource_access(org_id, ''project'', project_id))',
            target || '_epic36', target
        );
    END LOOP;
END
$rls$;

-- ---------------------------------------------------------------------------
-- 5. The privileges, WITH their REVOKE (migration 316's lesson).
-- ---------------------------------------------------------------------------

DO $grants$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'connector') THEN
        RAISE NOTICE
            '317: role `connector` does not exist on this cluster -- the '
            'append-only TRIGGER is the enforcement and is unaffected.';
        RETURN;
    END IF;

    -- The head carries a status and two pointers that move: UPDATE is its own.
    EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON app.context_relationships '
            'TO connector';

    -- The versions are append-only. INSERT and SELECT are the posture; DELETE
    -- is the RGPD hatch and nothing else; UPDATE is revoked BECAUSE 207 already
    -- granted it and a narrow GRANT alone would only have said otherwise.
    EXECUTE 'GRANT SELECT, INSERT, DELETE ON app.context_relationship_versions '
            'TO connector';
    EXECUTE 'REVOKE UPDATE ON app.context_relationship_versions FROM connector';
END
$grants$;

-- ---------------------------------------------------------------------------
-- 6. The backfill. Deterministic: every id is derived from the incumbent edge
--    id by sha256, so a re-run mints the same ids and inserts nothing new.
--    `sha256(bytea)` is core since PostgreSQL 11 -- no pgcrypto dependency --
--    and uppercase hex is a strict subset of Crockford base32, so the derived
--    ids satisfy the id CHECKs above.
--
--    The content hash is the SAME document `context_relationships.relationship_hash`
--    builds in Python: seven fields joined by newline, so the backfilled row and
--    a row minted through the service agree byte for byte. A pg test pins it.
-- ---------------------------------------------------------------------------

INSERT INTO app.context_relationships
    (id, org_id, project_id, source_type, source_id, target_type, target_id,
     relationship_kind, status, provenance, projection_edge_id,
     created_by, created_at, updated_at)
SELECT
    'crel_' || upper(substr(encode(sha256(convert_to(e.id || ':crel', 'UTF8')), 'hex'), 1, 26)),
    p.org_id,
    e.project_id,
    e.from_type,
    e.from_id,
    e.to_type,
    e.to_id,
    e.edge_type,
    'active',
    'backfill:context_graph',
    e.id,
    e.created_by,
    e.created_at,
    e.created_at
FROM app.context_graph e
LEFT JOIN app.projects p ON p.id = e.project_id
ON CONFLICT (id) DO NOTHING;

INSERT INTO app.context_relationship_versions
    (id, relationship_id, org_id, project_id, version_number, lifecycle, fact,
     supersedes_version_id, source_version_id, target_version_id,
     content_hash, created_by, created_at)
SELECT
    'crelv_' || upper(substr(encode(sha256(convert_to(r.id || ':crelv:1', 'UTF8')), 'hex'), 1, 26)),
    r.id,
    r.org_id,
    r.project_id,
    1,
    'created',
    jsonb_build_object(
        'source', jsonb_build_object('type', r.source_type, 'id', r.source_id),
        'target', jsonb_build_object('type', r.target_type, 'id', r.target_id),
        'kind', r.relationship_kind,
        'provenance', r.provenance
    ),
    NULL,
    NULL,
    NULL,
    encode(
        sha256(
            convert_to(
                concat_ws(
                    E'\n',
                    'context-relationship.v1',
                    COALESCE(r.project_id, ''),
                    r.source_type, r.source_id,
                    r.target_type, r.target_id,
                    r.relationship_kind
                ),
                'UTF8'
            )
        ),
        'hex'
    ),
    r.created_by,
    r.created_at
FROM app.context_relationships r
WHERE r.provenance = 'backfill:context_graph'
ON CONFLICT (id) DO NOTHING;

UPDATE app.context_relationships r
   SET current_version_id = v.id
  FROM app.context_relationship_versions v
 WHERE v.relationship_id = r.id
   AND v.version_number = 1
   AND r.current_version_id IS NULL;

-- Every incumbent edge is carried, or the migration has already failed above.
DO $proof$
DECLARE
    edges     BIGINT;
    carried   BIGINT;
BEGIN
    SELECT count(*) INTO edges FROM app.context_graph;
    SELECT count(*) INTO carried
      FROM app.context_relationships
     WHERE provenance = 'backfill:context_graph';

    IF carried <> edges THEN
        RAISE EXCEPTION
            '317: % context_graph edge(s) but % relation(s) carried. The '
            'backfill never drops and never merges -- refusing to commit a '
            'partial cutover.', edges, carried;
    END IF;
END
$proof$;

COMMENT ON TABLE app.context_relationships IS
    'Story 49-6 AC5: the one authority for a cross-owner context relation. '
    'app.context_graph becomes a READ projection written here in the same '
    'transaction (governance.md Decision 2). A relation whose endpoints are '
    'both Master Data objects belongs to the 49.2 service and is refused here.';

COMMENT ON TABLE app.context_relationship_versions IS
    'Story 49-6 AC5: append-only. One row per act (created / superseded / '
    'restored). `superseded_by` is DERIVED from the successor''s '
    '`supersedes_version_id`; storing it would require the UPDATE the trigger '
    'refuses.';

COMMIT;
