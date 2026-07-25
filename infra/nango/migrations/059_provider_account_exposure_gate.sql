-- Story 36.1: strict provider-account exposure lifecycle and Epic 36 RLS gate.
-- Additive and replayable. Provider/account identifiers remain opaque.

BEGIN;

ALTER TABLE app.credential_accounts
    ADD COLUMN IF NOT EXISTS available BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE app.credential_accounts
    ADD COLUMN IF NOT EXISTS discovery_generation BIGINT NOT NULL DEFAULT 0;
ALTER TABLE app.credential_accounts
    ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

ALTER TABLE app.credential_account_grants
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE app.credential_account_grants
    ADD COLUMN IF NOT EXISTS invalidated_at TIMESTAMPTZ;
ALTER TABLE app.credential_account_grants
    ADD COLUMN IF NOT EXISTS invalidation_reason TEXT;
ALTER TABLE app.credential_account_grants
    ADD COLUMN IF NOT EXISTS exposure_version BIGINT NOT NULL DEFAULT 1;
ALTER TABLE app.credential_account_grants
    ADD COLUMN IF NOT EXISTS policy_version TEXT NOT NULL DEFAULT 'v1';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'credential_account_grants_status_check'
    ) THEN
        ALTER TABLE app.credential_account_grants
            ADD CONSTRAINT credential_account_grants_status_check
            CHECK (status IN ('active', 'invalidated'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS credential_account_grants_active_exact
    ON app.credential_account_grants
       (credential_id, external_account_id, grantee_org_id)
    WHERE status = 'active';

CREATE OR REPLACE FUNCTION app.epic36_identity()
RETURNS TEXT
LANGUAGE sql
STABLE
AS $$
    SELECT NULLIF(current_setting('toorow.identity', true), '')
$$;

CREATE OR REPLACE FUNCTION app.epic36_has_resource_access(
    target_org TEXT,
    target_scope_type TEXT,
    target_scope_id TEXT
) RETURNS BOOLEAN
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = app, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM app.org_members m
        WHERE m.org_id = target_org
          AND m.identity = app.epic36_identity()
          AND m.status = 'active'
          AND (
              m.role = 'owner'
              OR EXISTS (
                  SELECT 1 FROM app.resource_grants g
                  WHERE g.org_id = target_org
                    AND g.identity = m.identity
                    AND g.scope_type = target_scope_type
                    AND g.scope_id = target_scope_id
              )
          )
    )
$$;

ALTER TABLE app.projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.projects FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS epic36_projects_strict ON app.projects;
CREATE POLICY epic36_projects_strict ON app.projects
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', id)
    );

ALTER TABLE app.datastreams ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.datastreams FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS epic36_datastreams_strict ON app.datastreams;
CREATE POLICY epic36_datastreams_strict ON app.datastreams
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'flux', id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'flux', id)
    );

COMMIT;
