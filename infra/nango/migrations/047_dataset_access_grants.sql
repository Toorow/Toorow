-- infra/nango/migrations/047_dataset_access_grants.sql
--
-- Story 24.5: Grant/revoke client access to the org marts dataset (Epic 24).
--
-- A ``dataset_access_grant`` lets an external BigQuery principal (a user, a
-- service account or a group) read the marts dataset of ONE organisation.  The
-- grant is ALWAYS scoped to ``org_<wslug>_marts`` -- it NEVER targets raw nor
-- mirror_* datasets (invariant enforced in warehouse_tenancy._simulate_bq_iam_grant
-- and proven by the test suite).
--
-- Soft-delete design: revoke sets ``revoked_at`` (the row survives a revoke so
-- an org's grant history stays queryable while the org exists).  The partial
-- unique index on (org_id, principal) WHERE revoked_at IS NULL prevents two
-- active grants for the same principal on the same org, while still allowing
-- re-granting after a revoke (a new row, not a row resurrection).
--
-- ON DELETE CASCADE (epic-24 review X-1): deleting the ORG erases its grant
-- rows in the same transaction -- that is RGPD-erasure by design, NOT a loss of
-- trace: _delete_org emits one DATASET_ACCESS_REVOKED audit entry per active
-- grant (reason=org_deleted) before committing, and the audit table is the
-- durable trail (retention policy: audit rows survive org deletion on purpose).
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/047_dataset_access_grants.sql
--
-- Schema-Change-Checklist: additive & idempotent; new table with IF NOT EXISTS;
-- indexes with IF NOT EXISTS.  No existing table is altered.  This is migration 047.
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- ---------------------------------------------------------------------------
-- dataset_access_grants: cross-org bridge for BigQuery IAM (marts only).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.dataset_access_grants (
    id          TEXT        PRIMARY KEY,           -- prefixed ULID: 'dagrant_<ULID>'
    org_id      TEXT        NOT NULL
                REFERENCES app.organizations(id) ON DELETE CASCADE,
    principal   TEXT        NOT NULL,              -- IAM member: type:identifier
    granted_by  TEXT        NOT NULL,              -- identity subject (AD-14)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at  TIMESTAMPTZ NULL                   -- NULL = active; set on revoke (RGPD)
);

-- Simple lookup index: list active grants for an org.
CREATE INDEX IF NOT EXISTS dataset_access_grants_org
    ON app.dataset_access_grants (org_id);

-- Composite index: fast filter on (org_id, revoked_at) for history/audit queries.
CREATE INDEX IF NOT EXISTS dataset_access_grants_org_revoked
    ON app.dataset_access_grants (org_id, revoked_at);

-- Partial unique index: one active grant per (org, principal).
-- Re-granting after revocation creates a new row (revoked row keeps revoked_at IS NOT NULL).
CREATE UNIQUE INDEX IF NOT EXISTS dataset_access_grants_uq_active
    ON app.dataset_access_grants (org_id, principal)
    WHERE revoked_at IS NULL;

COMMIT;
