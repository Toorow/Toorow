-- Story 49.2, AC1: the business taxonomy converges into the Master Data
-- authority, and it converges BY SUPERSESSION.
--
-- WHAT WAS MEASURED, 2026-08-25, before a line of this was written:
--
--   governance_read_model.py:446  the `classifications` lens reads
--                                 app.mdm_business_classifications
--   governance_read_model.py:665  the `registries` lens reads
--                                 app.master_data_registries
--   business_taxonomy.py:1039     INSERT INTO app.mdm_business_links
--   business_taxonomy.py:1122     DELETE FROM app.mdm_business_links
--
-- Two stores under ONE section of the console, and one of them destroys its
-- rows. That is the whole of AC1's "one extensible Master Data authority
-- replaces parallel models", still open twenty-six days after migration 143
-- built the schema that was meant to receive them.
--
-- WHY 143 STOPPED SHORT, AND WHY THIS IS NOT THAT. 143's own section 6 says it:
-- "a backfill that guesses which classification is a Product and which is an
-- Activity is precisely what the Implementation Gate forbids". It is right, and
-- nothing here guesses. The Gate's item 4 states the mapping in as many words --
-- *"migrate ambiguous business-classification rows as classifications, never
-- guess that they are Products or Activities"* -- so a classification becomes a
-- classification, a domain becomes a domain, and `classification_type` is
-- CARRIED in the version payload rather than interpreted. The guess the Gate
-- forbids is a later, human decision; moving the rows is not that decision.
--
-- WHAT THIS MIGRATION LANDS, AND WHAT IT DELIBERATELY DOES NOT.
--
-- It lands the three columns a supersession needs, on the three tables that
-- carry the parallel authority. It moves NO row: the rows move under
-- `core.master_data_convergence`, a durable operation with an actor, a reason,
-- an audit row and an idempotency key -- because "which of my classifications
-- is really a Product" is a question only the organization that wrote them can
-- answer, and a migration answers to nobody.
--
-- Additive and idempotent. No table is dropped. No row is deleted. The one
-- destructive statement in this file is the DROP of a UNIQUE CONSTRAINT that is
-- immediately replaced by a partial UNIQUE INDEX with strictly more meaning.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. A superseded taxonomy row stays readable and NAMES its successor.
--
-- The node keeps the row's own id (143 widened `master_data_nodes_id_check` to
-- accept `bd_`/`bcl_` for exactly this), so `superseded_by_node_id` is usually
-- equal to `id`. It is stored anyway, and the reason is the one this
-- repository keeps re-learning: a pointer that is derivable today stops being
-- derivable the first time a merge gives a row a survivor with a different id,
-- and by then the rows that needed it have already been written without it.
--
-- `superseded_by_operation_id` is what makes the convergence auditable from the
-- SOURCE side. `app.operations` already records the act; without this column a
-- reader holding a taxonomy row has no way back to it, and "when did this
-- become a governed node, and who decided" is answerable only by scanning the
-- audit log for something that mentions the id.
-- ---------------------------------------------------------------------------
ALTER TABLE app.mdm_business_domains
    ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ;
ALTER TABLE app.mdm_business_domains
    ADD COLUMN IF NOT EXISTS superseded_by_node_id TEXT;
ALTER TABLE app.mdm_business_domains
    ADD COLUMN IF NOT EXISTS superseded_by_operation_id TEXT;

ALTER TABLE app.mdm_business_domains
    DROP CONSTRAINT IF EXISTS ck_mdm_business_domains_supersession_complete;
ALTER TABLE app.mdm_business_domains
    ADD CONSTRAINT ck_mdm_business_domains_supersession_complete
    CHECK ((superseded_at IS NULL) = (superseded_by_node_id IS NULL));

ALTER TABLE app.mdm_business_classifications
    ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ;
ALTER TABLE app.mdm_business_classifications
    ADD COLUMN IF NOT EXISTS superseded_by_node_id TEXT;
ALTER TABLE app.mdm_business_classifications
    ADD COLUMN IF NOT EXISTS superseded_by_operation_id TEXT;

ALTER TABLE app.mdm_business_classifications
    DROP CONSTRAINT IF EXISTS ck_mdm_business_classifications_supersession_complete;
ALTER TABLE app.mdm_business_classifications
    ADD CONSTRAINT ck_mdm_business_classifications_supersession_complete
    CHECK ((superseded_at IS NULL) = (superseded_by_node_id IS NULL));

-- No foreign key to app.master_data_nodes on purpose. The two tables are joined
-- by MEANING, not by referential integrity: an organization erasure walks the FK
-- graph, and a key from the legacy store into the authority would put the node
-- on the erasure path of a row that is only a historical trace of it. The
-- convergence command verifies the node exists at write time, which is where
-- the check belongs -- the row it writes is never edited again.

CREATE INDEX IF NOT EXISTS idx_mdm_business_domains_live
    ON app.mdm_business_domains (org_id, status) WHERE superseded_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_mdm_business_classifications_live
    ON app.mdm_business_classifications (org_id, domain_id) WHERE superseded_at IS NULL;

COMMENT ON COLUMN app.mdm_business_domains.superseded_at IS
    'Story 49.2 AC1: this row is no longer the authority for its identity -- the Master Data node named beside it is. The row stays readable; nothing that pinned it stops resolving.';
COMMENT ON COLUMN app.mdm_business_classifications.superseded_at IS
    'Story 49.2 AC1: this row is no longer the authority for its identity -- the Master Data node named beside it is. The row stays readable; nothing that pinned it stops resolving.';

-- ---------------------------------------------------------------------------
-- 2. A governed business link is RETIRED, never destroyed.
--
-- `business_taxonomy.delete_link` ran `DELETE FROM app.mdm_business_links`. The
-- rule context-hub.md already carries for a manual event applies here word for
-- word -- *"Retirement is a supersede, never a delete. An event that has been
-- read is evidence"* -- and a business link is read by more surfaces than an
-- event is: the Datastream workbench's business path (datastream_workbench.py
-- :713), the Project's domain applicability (project_settings.py:1434), the
-- Context Hub graph, and the used-by count of every Business Domain and
-- classification in the Governance collection.
--
-- Destroying the row loses the difference between "this link was never made"
-- and "this link was made, and withdrawn" -- and the second is the one an
-- operator needs when a Datastream stops appearing under a domain.
-- ---------------------------------------------------------------------------
ALTER TABLE app.mdm_business_links
    ADD COLUMN IF NOT EXISTS retired_at TIMESTAMPTZ;
ALTER TABLE app.mdm_business_links
    ADD COLUMN IF NOT EXISTS retired_by TEXT;
ALTER TABLE app.mdm_business_links
    ADD COLUMN IF NOT EXISTS retired_reason TEXT;

ALTER TABLE app.mdm_business_links
    DROP CONSTRAINT IF EXISTS ck_mdm_business_links_retirement_named;
ALTER TABLE app.mdm_business_links
    ADD CONSTRAINT ck_mdm_business_links_retirement_named
    CHECK ((retired_at IS NULL) = (retired_by IS NULL));

-- The uniqueness of 130 covered every row, retired ones included, so a link
-- withdrawn by mistake could never be made again -- the exact repair path the
-- context-hub amendment names ("a retirement filed by mistake is repaired by
-- writing it again") would have raised a duplicate. One LIVE link per
-- (project, source, target, relation); the retired ones stack up behind it.
ALTER TABLE app.mdm_business_links
    DROP CONSTRAINT IF EXISTS uq_mdm_business_link;
CREATE UNIQUE INDEX IF NOT EXISTS uq_mdm_business_link_live
    ON app.mdm_business_links
       (project_id, taxonomy_type, taxonomy_id, target_type, target_id, relation_type)
    WHERE retired_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_mdm_business_links_taxonomy_live
    ON app.mdm_business_links (org_id, project_id, taxonomy_type, taxonomy_id)
    WHERE retired_at IS NULL;

COMMENT ON COLUMN app.mdm_business_links.retired_at IS
    'Story 49.2 AC1: a withdrawn link stops being SERVED and stays readable. Every live read filters on retired_at IS NULL; a caller that asks for withdrawn links gets them carrying their retirement.';

-- ---------------------------------------------------------------------------
-- 3. The scope trigger must not fire on a retirement.
--
-- `trg_mdm_business_links_scope` is BEFORE INSERT OR UPDATE, and
-- `validate_business_link_scope` demands that the link's taxonomy source still
-- be `status = 'active'`. Retiring the link of an ARCHIVED domain -- which is
-- precisely when an operator wants to retire it -- would therefore have been
-- refused, with a message about the SOURCE while the caller was withdrawing the
-- LINK.
--
-- The trigger is re-declared on the columns it actually validates. A retirement
-- touches none of them, so it no longer re-opens a question that was settled
-- when the link was created.
-- ---------------------------------------------------------------------------
DROP TRIGGER IF EXISTS trg_mdm_business_links_scope ON app.mdm_business_links;
CREATE TRIGGER trg_mdm_business_links_scope
    BEFORE INSERT OR UPDATE OF
        org_id, project_id, taxonomy_type, taxonomy_id,
        target_type, target_id, relation_type
    ON app.mdm_business_links
    FOR EACH ROW EXECUTE FUNCTION app.validate_business_link_scope();

-- ---------------------------------------------------------------------------
-- 4. The hard delete cannot come back.
--
-- Removing the DELETE from one function repairs one caller. The guard below
-- repairs the class: any statement that tries to destroy a governed business
-- link is refused, and names the gesture that is meant instead.
--
-- The erasure hatch is the one migrations 098/099 established and 143 had to
-- repair on the Master Data tables after 140 forgot it: an organization erasure
-- runs with `app.rgpd_erasure = on` and every protective DELETE trigger yields
-- to it. Without this clause the first real erasure request on an organization
-- holding one business link would fail, and nobody would learn that until the
-- request arrived.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.reject_business_link_deletion()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'a governed business link is retired, never deleted: set retired_at, retired_by and retired_reason'
        USING ERRCODE = 'raise_exception';
END;
$$;

DROP TRIGGER IF EXISTS trg_mdm_business_links_no_hard_delete ON app.mdm_business_links;
CREATE TRIGGER trg_mdm_business_links_no_hard_delete
    BEFORE DELETE ON app.mdm_business_links
    FOR EACH ROW
    WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
    EXECUTE FUNCTION app.reject_business_link_deletion();

COMMIT;
