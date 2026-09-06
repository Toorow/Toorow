-- A pin the validator accepts is a pin the foreign key can hold (AI-338).
--
-- THE DEFECT, MEASURED 2026-08-31 ON A DISPOSABLE POSTGRES AT MIGRATION 328.
-- `core.golden_questions._check_business_domain_pin` (golden_questions.py:468)
-- judges a `(domain_id, version_number)` pin against
-- `business_identity_catalogue.DOMAIN_VERSION_SOURCE` -- the UNION of the two
-- ledgers that number a Business Domain: the authority
-- (`app.master_data_object_versions`, published node revisions) and the
-- superseded ledger (`app.mdm_business_domain_versions`). Five foreign keys
-- judge the SAME pin against the superseded ledger ALONE. The two disagree
-- exactly where the product now mints identities, so the validator accepts and
-- the key then refuses:
--
--     [1] native Business Domain minted in the authority: bd_01M1CJRM2QP…
--         rows in legacy app.mdm_business_domains        : 1
--         rows in legacy app.mdm_business_domain_versions: 0
--         authority revisions                            : [1]
--     [2] _check_business_domain_pin refusals = []  pin=(bd_01M1CJRM2QP…, 1)
--     [3] INSERT REFUSED by the foreign key:
--         sqlstate   = 23503
--         constraint = fk_golden_question_versions_domain_version
--         detail     = Key is not present in table "mdm_business_domain_versions".
--
-- The gesture that dies there is the whole point of a Golden Question: pin the
-- meaning, then save the question. The person is told nothing -- the refusal is
-- a 500 out of a foreign key, about a version the console itself offered.
--
-- WHY IT IS PREEXISTING AND WHY IT SURFACES NOW. The five keys have pointed at
-- the superseded ledger since migrations 153 and 252. They were right while that
-- ledger was the only one numbering a domain. The convergence of 2026-08-25 gave
-- the authority its own numbering, story 49.2 re-pointed the READERS at the
-- union, and nothing re-pointed the KEYS. Migration 327 then made the collision
-- between the two numberings impossible, which is what makes the union safe to
-- key on today.
--
-- ---------------------------------------------------------------------------
-- THE FIVE KEYS, READ FROM `pg_constraint` AND NOT FROM THE MIGRATIONS
-- ---------------------------------------------------------------------------
--   evaluation_run_cases      fk_evaluation_run_cases_domain_version
--   feedback_annotations      fk_feedback_annotations_domain_version
--   feedback_regression_cases fk_feedback_regression_cases_domain
--   golden_question_versions  fk_golden_question_versions_domain_version
--   observed_cohorts          fk_observed_cohorts_domain_version
--
-- All five name `(domain_id, version_number)` of `app.mdm_business_domain_
-- versions`. NO foreign key anywhere points at
-- `app.mdm_business_classification_versions`, so the class is these five and
-- nothing else. The `…_domain_org` keys beside them, onto
-- `app.mdm_business_domains (id, org_id)`, are NOT part of the defect: the
-- creation command already writes the identity row born superseded
-- (`master_data_convergence.write_superseded_projection`, governance.md
-- § Decision 2), which the measurement above confirms -- 1 row. It is the
-- VERSION half of that projection that was never written, and deliberately so.
--
-- ---------------------------------------------------------------------------
-- THE FORM, AND THE TWO THAT WERE REFUSED
-- ---------------------------------------------------------------------------
-- (a) PROJECT THE AUTHORITY'S REVISION INTO THE SUPERSEDED LEDGER, the way the
--     identity is already projected. Cheapest by far -- no key moves. REFUSED:
--     it contradicts a ratified decision. governance.md § Decision 2 says in its
--     own words *"What the projection deliberately does NOT carry: a legacy
--     version row […] Named here rather than papered over by re-opening a
--     version ledger the cutover deliberately deleted the writer of."* Taking it
--     would need Jean to arbitrate against his own amendment, not a migration.
--
-- (b) A UNION VIEW THE KEYS POINT AT. Not available: Postgres references a
--     table, never a view or a matview, whatever index it carries.
--
-- (c) WHAT THIS MIGRATION DOES -- one derived table holding exactly what a
--     foreign key needs, and the five keys re-pointed at it. It is the same
--     shape the repository already ratified twice for the same reason: a read
--     projection written beside the row it mirrors (`app.context_graph`,
--     context-hub.md, amendment of 2026-08-28). Its cost is stated rather than
--     hidden: it STORES A DERIVABLE FACT, which CLAUDE.md tells us not to do, and
--     the reason is that Postgres offers no other way to key on a union. The
--     assertion at the bottom is what keeps the derivation honest, and
--     `test_the_pin_registry_is_the_union_it_claims_to_be` re-runs it per org.
--
-- WHAT IT DOES NOT DECIDE. Not AI-324. The registry answers ONE question --
-- *is this `(identity, number)` a pair some ledger numbers?* -- which is the
-- question a foreign key asks and the only one it asks. It holds no content, no
-- source and no precedence, and a pair reached from both ledgers is registered
-- ONCE (`ON CONFLICT DO NOTHING`), so nothing here asserts which content a
-- number designates. That remains `business_identity_catalogue`'s anti-masking
-- clause and migration 327's renumbering.
--
-- WHAT IT DOES NOT DO, NAMED. A renumbering -- the act migration 327 performed
-- with `trg_master_data_versions_protect` switched off -- would register the new
-- number and leave the old one behind. The registry is a SUPERSET in that one
-- case, so a key would hold a pin at a number nothing designates any more. It is
-- named here rather than guarded because the protect trigger makes a
-- renumbering impossible outside a migration, and a migration that renumbers
-- again must clean this table in the same transaction.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. The registry. Two columns for the key, `org_id` for the tenant tree, and
--    nothing else -- a third column would be a fact with two homes.
-- ---------------------------------------------------------------------------
--
-- `org_id` carries the foreign key onto `app.organizations` so that
-- `core.org_purge` -- which reads the blocking-edge graph from `pg_constraint`
-- at call time rather than a hardcoded list -- reaches this table on its own and
-- deletes it in the right order: after the five tables that now point AT it.
-- Without that edge an RGPD erasure would stop on a table nobody named.
--
-- There is deliberately NO foreign key on `domain_id`. Pointing it at
-- `app.mdm_business_domains` would make this table depend on the identity
-- projection, which governance.md § Decision 2 describes as serving the
-- pre-convergence organization "and nothing else" -- a dependency on a row whose
-- retirement is already planned.
CREATE TABLE IF NOT EXISTS app.business_domain_version_registry (
    domain_id       TEXT        NOT NULL,
    version_number  INTEGER     NOT NULL,
    org_id          TEXT        NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    registered_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pk_business_domain_version_registry
        PRIMARY KEY (domain_id, version_number),
    CONSTRAINT ck_business_domain_version_registry_number
        CHECK (version_number >= 1)
);

CREATE INDEX IF NOT EXISTS idx_business_domain_version_registry_org
    ON app.business_domain_version_registry (org_id, domain_id);

COMMENT ON TABLE app.business_domain_version_registry IS
    'Which (Business Domain, version number) pairs a ledger numbers -- the '
    'authority (app.master_data_object_versions, published node revisions) and '
    'the superseded ledger (app.mdm_business_domain_versions). Derived, never '
    'authored: it exists because a foreign key references a table and the '
    'validators judge a union. It holds no content and decides no precedence.';

-- Migration 207's `ALTER DEFAULT PRIVILEGES` already hands SELECT, INSERT,
-- UPDATE, DELETE to `connector` on every future table in `app`, so the GRANT
-- below is declarative (migration 316's finding). The REVOKE is the sentence,
-- and it is written the day the table is created, which is the only day it costs
-- nothing: nothing UPDATEs a registered pair -- a pair is registered or it is
-- not -- and the one path that DELETEs is the org purge, which runs as the
-- application role.
GRANT SELECT, INSERT, DELETE ON app.business_domain_version_registry TO connector;
REVOKE UPDATE ON app.business_domain_version_registry FROM connector;

-- The policy is the one the superseded ledger carries, character for character,
-- because this table holds the same facts about the same tenants -- including
-- the `platform` organization, which owns the six reference domains shipped with
-- the product and has no member by construction (migration 273). Referential
-- integrity checks bypass row security, so a pin never fails to see the pair it
-- names; the policy governs who may READ the registry, which is the only thing
-- it can govern.
ALTER TABLE app.business_domain_version_registry ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.business_domain_version_registry FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS business_domain_version_registry_epic36
    ON app.business_domain_version_registry;
CREATE POLICY business_domain_version_registry_epic36
    ON app.business_domain_version_registry
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_is_org_member(org_id) OR org_id = 'platform');

-- ---------------------------------------------------------------------------
-- 2. The backfill, both ledgers. This runs BEFORE the keys move, so the ALTER
--    below validates the pins that already exist instead of refusing them.
-- ---------------------------------------------------------------------------
INSERT INTO app.business_domain_version_registry (domain_id, version_number, org_id)
SELECT legacy.domain_id, legacy.version_number, legacy.org_id
  FROM app.mdm_business_domain_versions legacy
ON CONFLICT DO NOTHING;

-- `published_at IS NOT NULL` is the same line the catalogue draws
-- (`_AUTHORITY_REVISION_EXISTS`): a draft revision is not history, and it is the
-- only kind of node version that can still be DELETEd
-- (`app.protect_master_data_object_version` refuses to delete anything else). A
-- registry holding drafts would let a pin outlive the revision it names.
INSERT INTO app.business_domain_version_registry (domain_id, version_number, org_id)
SELECT mdv.node_id, mdv.version_number, mdv.org_id
  FROM app.master_data_object_versions mdv
  JOIN app.master_data_nodes n
    ON n.id = mdv.node_id AND n.org_id = mdv.org_id AND n.project_id IS NULL
 WHERE n.node_kind = 'business_domain'
   AND mdv.published_at IS NOT NULL
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------------------
-- 3. The two writers. Triggers rather than application code, because the ledgers
--    have writers this repository does not route through one module: migration
--    130's `trg_organizations_seed_business_domains` seeds six domains and six
--    versions at organization creation, `core.business_taxonomy` still writes for
--    an organization that has not converged, and the test fixtures INSERT
--    directly. A Python writer would be true for the paths it was added to and
--    silently false for the others -- which is the exact shape of the defect this
--    migration repairs.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.register_business_domain_version()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO app.business_domain_version_registry
        (domain_id, version_number, org_id)
    VALUES (NEW.domain_id, NEW.version_number, NEW.org_id)
    ON CONFLICT DO NOTHING;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_mdm_business_domain_versions_register
    ON app.mdm_business_domain_versions;
CREATE TRIGGER trg_mdm_business_domain_versions_register
    AFTER INSERT ON app.mdm_business_domain_versions
    FOR EACH ROW EXECUTE FUNCTION app.register_business_domain_version();

-- The authority's side fires on INSERT and on UPDATE, because a node revision is
-- born a draft and becomes history later: `master_data.publish_node_version` is
-- an UPDATE that stamps `published_at`. A trigger on INSERT alone would register
-- nothing the authority ever mints.
CREATE OR REPLACE FUNCTION app.register_node_version_as_business_domain()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.node_id IS NULL OR NEW.published_at IS NULL THEN
        RETURN NULL;
    END IF;
    INSERT INTO app.business_domain_version_registry
        (domain_id, version_number, org_id)
    SELECT NEW.node_id, NEW.version_number, NEW.org_id
      FROM app.master_data_nodes n
     WHERE n.id = NEW.node_id
       AND n.org_id = NEW.org_id
       AND n.project_id IS NULL
       AND n.node_kind = 'business_domain'
    ON CONFLICT DO NOTHING;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_master_data_versions_register_domain
    ON app.master_data_object_versions;
CREATE TRIGGER trg_master_data_versions_register_domain
    AFTER INSERT OR UPDATE ON app.master_data_object_versions
    FOR EACH ROW EXECUTE FUNCTION app.register_node_version_as_business_domain();

-- ---------------------------------------------------------------------------
-- 4. The five keys move. Each DROP is written beside its ADD so a reader sees
--    the pair, and the ADD is not `NOT VALID`: the backfill above is what makes
--    every existing pin verifiable, and a key nobody validated would let this
--    migration pass while the defect stayed.
-- ---------------------------------------------------------------------------
ALTER TABLE app.golden_question_versions
    DROP CONSTRAINT fk_golden_question_versions_domain_version;
ALTER TABLE app.golden_question_versions
    ADD CONSTRAINT fk_golden_question_versions_domain_version
    FOREIGN KEY (business_domain_id, business_domain_version_number)
    REFERENCES app.business_domain_version_registry (domain_id, version_number);

ALTER TABLE app.observed_cohorts
    DROP CONSTRAINT fk_observed_cohorts_domain_version;
ALTER TABLE app.observed_cohorts
    ADD CONSTRAINT fk_observed_cohorts_domain_version
    FOREIGN KEY (business_domain_id, business_domain_version)
    REFERENCES app.business_domain_version_registry (domain_id, version_number);

ALTER TABLE app.evaluation_run_cases
    DROP CONSTRAINT fk_evaluation_run_cases_domain_version;
ALTER TABLE app.evaluation_run_cases
    ADD CONSTRAINT fk_evaluation_run_cases_domain_version
    FOREIGN KEY (business_domain_id, business_domain_version_number)
    REFERENCES app.business_domain_version_registry (domain_id, version_number);

ALTER TABLE app.feedback_annotations
    DROP CONSTRAINT fk_feedback_annotations_domain_version;
ALTER TABLE app.feedback_annotations
    ADD CONSTRAINT fk_feedback_annotations_domain_version
    FOREIGN KEY (business_domain_id, business_domain_version_number)
    REFERENCES app.business_domain_version_registry (domain_id, version_number);

ALTER TABLE app.feedback_regression_cases
    DROP CONSTRAINT fk_feedback_regression_cases_domain;
ALTER TABLE app.feedback_regression_cases
    ADD CONSTRAINT fk_feedback_regression_cases_domain
    FOREIGN KEY (business_domain_id, business_domain_version_number)
    REFERENCES app.business_domain_version_registry (domain_id, version_number);

-- ---------------------------------------------------------------------------
-- 5. The state this migration exists to reach, asserted rather than hoped for.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    missing INTEGER;
    strays  INTEGER;
    keys    INTEGER;
BEGIN
    -- (a) The registry holds every pair the validators accept. A backfill that
    --     silently dropped a ledger would move the defect rather than close it.
    SELECT count(*) INTO missing FROM (
        SELECT legacy.domain_id AS id, legacy.version_number AS number
          FROM app.mdm_business_domain_versions legacy
        UNION
        SELECT mdv.node_id, mdv.version_number
          FROM app.master_data_object_versions mdv
          JOIN app.master_data_nodes n
            ON n.id = mdv.node_id AND n.org_id = mdv.org_id AND n.project_id IS NULL
         WHERE n.node_kind = 'business_domain' AND mdv.published_at IS NOT NULL
    ) AS numbered
    WHERE NOT EXISTS (
        SELECT 1 FROM app.business_domain_version_registry r
         WHERE r.domain_id = numbered.id AND r.version_number = numbered.number
    );
    IF missing > 0 THEN
        RAISE EXCEPTION
            '% numbered revision(s) are absent from the registry the five keys '
            'now judge -- a pin the validator accepts would still be refused',
            missing USING ERRCODE = '23503';
    END IF;

    -- (b) And it holds NOTHING ELSE. A registry wider than the union would let a
    --     key hold a pin no validator would ever have accepted, which is the
    --     same disagreement pointing the other way.
    SELECT count(*) INTO strays
      FROM app.business_domain_version_registry r
     WHERE NOT EXISTS (
            SELECT 1 FROM app.mdm_business_domain_versions legacy
             WHERE legacy.domain_id = r.domain_id
               AND legacy.version_number = r.version_number)
       AND NOT EXISTS (
            SELECT 1
              FROM app.master_data_object_versions mdv
              JOIN app.master_data_nodes n
                ON n.id = mdv.node_id AND n.org_id = mdv.org_id
               AND n.project_id IS NULL
             WHERE mdv.node_id = r.domain_id
               AND mdv.version_number = r.version_number
               AND n.node_kind = 'business_domain'
               AND mdv.published_at IS NOT NULL);
    IF strays > 0 THEN
        RAISE EXCEPTION
            '% registered pair(s) are numbered by neither ledger', strays
            USING ERRCODE = '23514';
    END IF;

    -- (c) Five keys judge the registry, and none still judges the ledger alone.
    SELECT count(*) INTO keys
      FROM pg_constraint c
     WHERE c.contype = 'f'
       AND c.confrelid = 'app.business_domain_version_registry'::regclass;
    IF keys <> 5 THEN
        RAISE EXCEPTION
            '% foreign key(s) point at the pin registry, expected 5', keys
            USING ERRCODE = '23000';
    END IF;

    SELECT count(*) INTO keys
      FROM pg_constraint c
     WHERE c.contype = 'f'
       AND c.confrelid = 'app.mdm_business_domain_versions'::regclass;
    IF keys <> 0 THEN
        RAISE EXCEPTION
            '% foreign key(s) still judge the superseded ledger alone', keys
            USING ERRCODE = '23000';
    END IF;
END $$;

COMMIT;
