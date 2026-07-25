-- infra/nango/migrations/037_credential_ownership_grants.sql
--
-- Story 21.3: Decouple the credential from the project (Epic 21, FR37/CAP-25).
--
-- The "credential" is the physical table app.connection_ref (NOT renamed -- it
-- keeps its technical identifier). Until now a credential was bound to ONE
-- project via connection_ref.project_id. This migration lets a credential be
-- OWNED by an organization and SHARED to other organizations one external
-- account at a time -- so a single Google auth exposing 50 accounts can serve
-- several client orgs while each org only ever sees the accounts exposed to it.
--
-- Structural isolation: a grant targets a (credential_id, external_account_id)
-- pair via a composite FK to credential_accounts -- it is IMPOSSIBLE to grant a
-- whole credential. The per-identity "who may expose" enforcement is Story 21.5.
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/037_credential_ownership_grants.sql
--
-- Schema-Change-Checklist: additive & idempotent; new column NULLable; backfill
-- re-runnable (WHERE owner_org_id IS NULL); connection_ref.project_id kept for
-- compat (removed in a later dedicated story). This is migration 037.
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- ---------------------------------------------------------------------------
-- 1. credential ownership: connection_ref.owner_org_id (+ backfill from project).
--    NULLable + FK RESTRICT (an org owning credentials cannot be hard-deleted).
--    project_id is kept for compat; owner_org_id is the new ownership axis.
-- ---------------------------------------------------------------------------
ALTER TABLE app.connection_ref ADD COLUMN IF NOT EXISTS owner_org_id TEXT;

-- Backfill: the credential's owner org = the org of its (compat) project.
-- Idempotent: only fills NULLs; re-run updates nothing.
UPDATE app.connection_ref cr
SET owner_org_id = p.org_id
FROM app.projects p
WHERE cr.project_id = p.id
  AND cr.owner_org_id IS NULL;

ALTER TABLE app.connection_ref DROP CONSTRAINT IF EXISTS fk_connection_ref_owner_org;
ALTER TABLE app.connection_ref
    ADD CONSTRAINT fk_connection_ref_owner_org
    FOREIGN KEY (owner_org_id) REFERENCES app.organizations(id) ON DELETE RESTRICT;

CREATE INDEX IF NOT EXISTS connection_ref_owner_org ON app.connection_ref (owner_org_id);

-- ---------------------------------------------------------------------------
-- 2. credential_accounts: the external accounts a credential exposes (e.g. the
--    50 GA4 properties one Google auth can see). Catalogued at discovery time.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.credential_accounts (
    credential_id        TEXT        NOT NULL
                         REFERENCES app.connection_ref(id) ON DELETE CASCADE,
    external_account_id  TEXT        NOT NULL,           -- e.g. GA4 property id
    label                TEXT,                           -- human-readable account name
    discovered_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_credential_accounts PRIMARY KEY (credential_id, external_account_id)
);
CREATE INDEX IF NOT EXISTS credential_accounts_credential
    ON app.credential_accounts (credential_id);

-- ---------------------------------------------------------------------------
-- 3. credential_account_grants: the ONLY cross-org bridge, granular to a single
--    account. The composite FK to credential_accounts makes it impossible to
--    grant a non-existent (or whole) credential; UNIQUE prevents duplicates.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.credential_account_grants (
    id                   TEXT        PRIMARY KEY,        -- prefixed ULID: 'cgrant_'
    credential_id        TEXT        NOT NULL,
    external_account_id  TEXT        NOT NULL,
    grantee_org_id       TEXT        NOT NULL
                         REFERENCES app.organizations(id) ON DELETE CASCADE,
    granted_by           TEXT        NOT NULL,           -- identity subject (AD-14)
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_credential_account_grants_account
        FOREIGN KEY (credential_id, external_account_id)
        REFERENCES app.credential_accounts (credential_id, external_account_id)
        ON DELETE CASCADE,
    CONSTRAINT uq_credential_account_grants
        UNIQUE (credential_id, external_account_id, grantee_org_id)
);
CREATE INDEX IF NOT EXISTS credential_account_grants_grantee
    ON app.credential_account_grants (grantee_org_id);
CREATE INDEX IF NOT EXISTS credential_account_grants_credential
    ON app.credential_account_grants (credential_id);

COMMIT;
