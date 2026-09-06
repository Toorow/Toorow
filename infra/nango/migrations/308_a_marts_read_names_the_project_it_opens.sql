-- infra/nango/migrations/308_a_marts_read_names_the_project_it_opens.sql
--
-- Story 62.3 -- governed OUTBOUND read on the published marts.
--
-- Until now a dataset-access grant could only name an ORGANIZATION, and the
-- dataset check enforced exactly that: ``org_<wslug>_marts``. A read opened for
-- one project therefore opened every project of the organization, which is the
-- opposite of what the person asked for.
--
-- This migration adds the PROJECT the read opens, the fact that the product
-- MINTED the service account (and must therefore retire it), and the moment
-- that account was retired. It also widens the dataset check by exactly one
-- shape -- ``marts_<project_id>`` -- and closes it on scratch names, which is
-- what a durable reporting cannot bind to.
--
-- It also creates ``app.mart_contract_versions``: the ONLY part of a mart
-- contract that is not derivable. The columns are declared with the product in
-- ``dbt/models/marts/*.yml`` and are derived at read time; what has to be
-- stored is the version an outside dashboard was told to expect, its
-- fingerprint, and the moment the version it replaced stops being readable.
--
-- Schema-Change-Checklist: additive and idempotent. Two CHECK constraints are
-- REPLACED (dropped then recreated wider); no existing row is invalidated --
-- every row that satisfied the old dataset check satisfies the new one, and
-- rows with a NULL dataset_id are untouched. No migration below this number is
-- edited. This is migration 308.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. The grant names the project it opens, and the account it owns.
-- ---------------------------------------------------------------------------

ALTER TABLE app.dataset_access_grants
    ADD COLUMN IF NOT EXISTS project_id TEXT NULL
        REFERENCES app.projects(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS managed_service_account BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS service_account_deleted_at TIMESTAMPTZ NULL;

CREATE INDEX IF NOT EXISTS dataset_access_grants_project
    ON app.dataset_access_grants (project_id)
    WHERE project_id IS NOT NULL;

-- One active read per (project, principal). The organization-scoped partial
-- unique index of migration 047 stays as it is for org-scoped rows; this one
-- covers the project-scoped rows it cannot see, so the same reader can be
-- opened on two DIFFERENT projects of the same organization without colliding.
CREATE UNIQUE INDEX IF NOT EXISTS dataset_access_grants_uq_active_project
    ON app.dataset_access_grants (project_id, principal)
    WHERE revoked_at IS NULL AND project_id IS NOT NULL;

DO $$
BEGIN
    -- The dataset check of migration 290 knew ONE legal shape. It now knows
    -- two, and refuses a scratch name in both.
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'dataset_access_grants_dataset_ck'
          AND conrelid = 'app.dataset_access_grants'::regclass
    ) THEN
        ALTER TABLE app.dataset_access_grants
            DROP CONSTRAINT dataset_access_grants_dataset_ck;
    END IF;
    ALTER TABLE app.dataset_access_grants
        ADD CONSTRAINT dataset_access_grants_dataset_ck CHECK (
            dataset_id IS NULL
            OR (
                (
                    (project_id IS NULL AND dataset_id LIKE 'org\_%\_marts' ESCAPE '\')
                    OR (project_id IS NOT NULL AND dataset_id = 'marts_' || project_id)
                )
                AND dataset_id NOT LIKE '%\_raw%' ESCAPE '\'
                AND dataset_id NOT LIKE 'mirror\_%' ESCAPE '\'
                AND dataset_id NOT LIKE '%staging%'
                AND dataset_id NOT LIKE '%sandbox%'
                AND dataset_id NOT LIKE '%scratch%'
                AND dataset_id NOT LIKE '%\_tmp\_%' ESCAPE '\'
                AND dataset_id NOT LIKE '%\_temp\_%' ESCAPE '\'
                AND dataset_id !~ '_(19|20)[0-9]{2}([_-]?(0[1-9]|1[0-2])([_-]?(0[1-9]|[12][0-9]|3[01]))?)$'
            )
        );

    -- A retired account is a fact about a REVOKED read: the product never
    -- deletes the account of a read that is still open.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'dataset_access_grants_managed_account_ck'
          AND conrelid = 'app.dataset_access_grants'::regclass
    ) THEN
        ALTER TABLE app.dataset_access_grants
            ADD CONSTRAINT dataset_access_grants_managed_account_ck CHECK (
                service_account_deleted_at IS NULL
                OR (managed_service_account AND lifecycle_state = 'revoked')
            );
    END IF;

    -- The product only mints service accounts, never users or groups.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'dataset_access_grants_managed_principal_ck'
          AND conrelid = 'app.dataset_access_grants'::regclass
    ) THEN
        ALTER TABLE app.dataset_access_grants
            ADD CONSTRAINT dataset_access_grants_managed_principal_ck CHECK (
                NOT managed_service_account
                OR principal LIKE 'serviceAccount:%'
            );
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. The contract a published mart offers to a reader outside the product.
-- ---------------------------------------------------------------------------
--
-- What is NOT here, on purpose: the column names. They are declared with the
-- product in the dbt marts catalogue and derived at read time -- a derivable
-- value is not a stored one. Only the fingerprint of the shape that was
-- published is kept, so a rebuild that changes a column is DETECTED rather
-- than believed.

CREATE TABLE IF NOT EXISTS app.mart_contract_versions (
    id                  TEXT        PRIMARY KEY,   -- prefixed ULID: 'martver_<ULID>'
    -- org_id is carried, not derived, for exactly one reason: migration 273's
    -- ratchet arms every org-scoped table from this column, and a table that
    -- omits it leaves the sweep in silence -- the mechanism 273 was written to
    -- end. The policy below is the 32-table form, word for word.
    org_id              TEXT        NOT NULL
                        REFERENCES app.organizations(id) ON DELETE CASCADE,
    project_id          TEXT        NOT NULL
                        REFERENCES app.projects(id) ON DELETE CASCADE,
    mart_table          TEXT        NOT NULL,
    schema_version      INTEGER     NOT NULL CHECK (schema_version >= 1),
    columns_fingerprint TEXT        NOT NULL,
    published_by        TEXT        NOT NULL,
    published_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- A superseded version stays READABLE until `readable_until`, so a
    -- dashboard bound yesterday keeps reading while its owner is told to move.
    superseded_at       TIMESTAMPTZ NULL,
    readable_until      TIMESTAMPTZ NULL,
    CONSTRAINT mart_contract_versions_superseded_ck CHECK (
        (superseded_at IS NULL) = (readable_until IS NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS mart_contract_versions_uq_version
    ON app.mart_contract_versions (project_id, mart_table, schema_version);

-- Exactly one current version per (project, mart).
CREATE UNIQUE INDEX IF NOT EXISTS mart_contract_versions_uq_current
    ON app.mart_contract_versions (project_id, mart_table)
    WHERE superseded_at IS NULL;

ALTER TABLE app.mart_contract_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.mart_contract_versions FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS mart_contract_versions_epic36 ON app.mart_contract_versions;
CREATE POLICY mart_contract_versions_epic36 ON app.mart_contract_versions
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id))
    WITH CHECK (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

GRANT SELECT, INSERT, UPDATE ON app.mart_contract_versions TO connector;

COMMIT;
