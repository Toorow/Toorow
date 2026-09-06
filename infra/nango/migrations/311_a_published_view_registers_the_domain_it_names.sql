-- The used-by store can name an ORGANIZATION-scoped identity.
--
-- WHAT WAS MEASURED, 2026-08-25, before a line of this was written:
--
--   app.master_data_used_by            0 rows
--   app.master_data_nodes              0 rows
--   app.mdm_business_domains          12 rows, 0 of them superseded
--   semantic_concept_versions         26 rows, 0 naming a Business Domain
--   semantic_view_versions             5 rows, 0 naming a Business Domain
--
-- WHY THIS EXISTS. `docs/product-architecture/governance.md` carries the hole in
-- as many words, in the *Incomplete if* of the 2026-08-25 cutover: *"a converged
-- Business Domain can be archived while a published Semantic Model version still
-- references it. The taxonomy writer refused that; the authority's impact guard
-- reads `app.master_data_used_by`, and no writer registers a Semantic Model
-- version's Business Domain reference there yet."*
--
-- The writer lands in `core.semantic_model_used_by`. It could not land against
-- this schema, and the reason is not the Python:
--
--   * a Business Domain converges to an ORGANIZATION-scoped node
--     (`master_data_convergence._apply` -> `create_org_node`), so its
--     `master_data_nodes.project_id` and its registry's `project_id` are NULL --
--     143 made both columns nullable for exactly that;
--   * `app.master_data_used_by.project_id` is NOT NULL, and 143 says why it
--     stays so: *"a consumer always lives in exactly one Project, whatever the
--     scope of the object it depends on"*;
--   * its two composite foreign keys are therefore ALWAYS enforced -- MATCH
--     SIMPLE only skips a key when one of ITS OWN columns is NULL, and neither
--     is. `(project_id, node_id) -> master_data_nodes(project_id, id)` can never
--     match a node whose `project_id` is NULL.
--
-- So the first Semantic View that named a converged domain would have been
-- refused at publication by a foreign key, and the guard would still be blind.
--
-- WHAT REPLACES THEM, AND WHY IT IS NOT LESS. This is 143's own section 2 move,
-- applied to the one table it did not reach: a single-column foreign key keeps
-- the LINK enforced at every scope, and what the composite key additionally
-- proved -- that the two rows belong to the same Project -- is restated as a
-- trigger, which can express the organization case the composite key cannot.
-- The trigger is strictly stronger than what it replaces: the composite key
-- proved same-Project and said nothing about the organization; this refuses a
-- Project of one organization declaring a dependency on another's identity, and
-- that boundary was previously enforced by nothing at all.
--
-- Additive for every row that exists: 0 rows in the table on 2026-08-25, and
-- every live writer (`observed_entities`, `market_governance`,
-- `governance_rule_sets`) registers a Project-scoped node of its own Project,
-- which the trigger accepts unchanged.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. The composite keys go, found by their COLUMN SET.
--
-- 140 declared them inline, so their names are generated and truncated at 63
-- characters. 143 hit the same wall on `master_data_object_versions` and solved
-- it the same way; guessing a name here would silently drop nothing and leave
-- the constraint in place, which is the failure mode that reads as success.
-- ---------------------------------------------------------------------------
DO $migration$
DECLARE
    victim text;
BEGIN
    FOR victim IN
        SELECT con.conname
        FROM pg_constraint con
        JOIN pg_class c ON c.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'app'
          AND c.relname = 'master_data_used_by'
          AND con.contype = 'f'
          AND (
              -- `attname` is `name`, not `text`; without the cast the comparison
              -- raises "operator does not exist: name[] = text[]".
              SELECT array_agg(att.attname::text ORDER BY att.attname::text)
              FROM unnest(con.conkey) AS k(attnum)
              JOIN pg_attribute att ON att.attrelid = con.conrelid
                                   AND att.attnum = k.attnum
          ) IN (
              ARRAY['project_id', 'registry_id'],
              ARRAY['node_id', 'project_id'],
              ARRAY['hierarchy_version_id', 'project_id']
          )
    LOOP
        EXECUTE format('ALTER TABLE app.master_data_used_by DROP CONSTRAINT %I', victim);
    END LOOP;
END
$migration$;

ALTER TABLE app.master_data_used_by
    DROP CONSTRAINT IF EXISTS fk_master_data_used_by_registry_any_scope;
ALTER TABLE app.master_data_used_by
    ADD CONSTRAINT fk_master_data_used_by_registry_any_scope
    FOREIGN KEY (registry_id) REFERENCES app.master_data_registries(id) ON DELETE RESTRICT;

ALTER TABLE app.master_data_used_by
    DROP CONSTRAINT IF EXISTS fk_master_data_used_by_node_any_scope;
ALTER TABLE app.master_data_used_by
    ADD CONSTRAINT fk_master_data_used_by_node_any_scope
    FOREIGN KEY (node_id) REFERENCES app.master_data_nodes(id) ON DELETE RESTRICT;

ALTER TABLE app.master_data_used_by
    DROP CONSTRAINT IF EXISTS fk_master_data_used_by_hierarchy_version_any_scope;
ALTER TABLE app.master_data_used_by
    ADD CONSTRAINT fk_master_data_used_by_hierarchy_version_any_scope
    FOREIGN KEY (hierarchy_version_id)
    REFERENCES app.master_data_object_versions(id) ON DELETE RESTRICT;

-- ---------------------------------------------------------------------------
-- 2. What the composite keys proved, restated where it can also cover the
--    organization scope.
--
-- Three rows are checked and each one asks the same two questions: does it
-- belong to a DIFFERENT Project than the consumer, and does it belong to a
-- DIFFERENT organization. A Project-scoped row must be this Project's; a
-- Project-less row (organization or platform scope) must be this Project's
-- organization's. The organization half is the confidentiality boundary the
-- Master Data core states in `master_data.py`: a Project surface reads its own
-- associations and nothing else, and a dependency row that could name another
-- tenant's identity would be one forgotten WHERE away from leaking it.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.validate_master_data_used_by_scope()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    consumer_org TEXT;
    row_project  TEXT;
    row_org      TEXT;
BEGIN
    SELECT p.org_id INTO consumer_org FROM app.projects p WHERE p.id = NEW.project_id;
    IF consumer_org IS NULL THEN
        RAISE EXCEPTION
            'a used-by consumer names a project that does not exist: %', NEW.project_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;

    SELECT r.project_id, r.org_id INTO row_project, row_org
      FROM app.master_data_registries r WHERE r.id = NEW.registry_id;
    IF row_project IS NOT NULL AND row_project <> NEW.project_id THEN
        RAISE EXCEPTION
            'used-by registry % is owned by project %, not by the consumer project %',
            NEW.registry_id, row_project, NEW.project_id
            USING ERRCODE = 'raise_exception';
    END IF;
    IF row_org IS DISTINCT FROM consumer_org THEN
        RAISE EXCEPTION
            'used-by registry % belongs to another organization than project %',
            NEW.registry_id, NEW.project_id
            USING ERRCODE = 'raise_exception';
    END IF;

    SELECT n.project_id, n.org_id INTO row_project, row_org
      FROM app.master_data_nodes n WHERE n.id = NEW.node_id;
    IF row_project IS NOT NULL AND row_project <> NEW.project_id THEN
        RAISE EXCEPTION
            'used-by node % is owned by project %, not by the consumer project %',
            NEW.node_id, row_project, NEW.project_id
            USING ERRCODE = 'raise_exception';
    END IF;
    IF row_org IS DISTINCT FROM consumer_org THEN
        RAISE EXCEPTION
            'used-by node % belongs to another organization than project %',
            NEW.node_id, NEW.project_id
            USING ERRCODE = 'raise_exception';
    END IF;

    IF NEW.hierarchy_version_id IS NOT NULL THEN
        SELECT v.project_id, v.org_id INTO row_project, row_org
          FROM app.master_data_object_versions v WHERE v.id = NEW.hierarchy_version_id;
        IF row_project IS NOT NULL AND row_project <> NEW.project_id THEN
            RAISE EXCEPTION
                'used-by hierarchy version % is owned by project %, not by %',
                NEW.hierarchy_version_id, row_project, NEW.project_id
                USING ERRCODE = 'raise_exception';
        END IF;
        IF row_org IS DISTINCT FROM consumer_org THEN
            RAISE EXCEPTION
                'used-by hierarchy version % belongs to another organization than project %',
                NEW.hierarchy_version_id, NEW.project_id
                USING ERRCODE = 'raise_exception';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_master_data_used_by_scope ON app.master_data_used_by;
CREATE TRIGGER trg_master_data_used_by_scope
    BEFORE INSERT OR UPDATE OF project_id, registry_id, node_id, hierarchy_version_id
    ON app.master_data_used_by
    FOR EACH ROW EXECUTE FUNCTION app.validate_master_data_used_by_scope();

-- ---------------------------------------------------------------------------
-- 3. The read the authority's archive guard makes.
--
-- 140's index is `(project_id, node_id) WHERE released_at IS NULL`, which is the
-- Project-scoped question. Archiving an organization identity asks a different
-- one -- "who depends on this node, in ANY Project of the organization" -- and
-- answers it through `master_data.assess_org_node_impact`, keyed by node alone.
-- Without this index that guard degrades to a sequential scan the day the table
-- stops being empty.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_master_data_used_by_node_any_scope
    ON app.master_data_used_by (node_id)
    WHERE released_at IS NULL;

COMMENT ON COLUMN app.master_data_used_by.project_id IS
    'The Project the CONSUMER lives in. Never the scope of the object depended on: an organization-scoped Business Domain is named here by the Project whose Semantic Model version references it.';
COMMENT ON COLUMN app.master_data_used_by.consumer_version_id IS
    'The exact version of the consumer that declared this dependency. A supersession releases the version it replaced by this column, never by consumer alone -- the two versions routinely name the same identity.';

COMMIT;
