-- Story 46.4: strict Project grants and one durable Getting Started journey.
BEGIN;

-- Preserve explicit legacy access before retiring the default-open pivot.
INSERT INTO app.org_members (id, org_id, identity, role, status, joined_at)
SELECT 'omem_46_' || md5(p.org_id || ':' || pm.identity), p.org_id, pm.identity,
       CASE WHEN pm.role = 'owner' THEN 'owner' ELSE 'member' END, 'active', NOW()
FROM app.project_members pm
JOIN app.projects p ON p.id = pm.project_id
WHERE p.org_id IS NOT NULL
ON CONFLICT (org_id, identity) DO NOTHING;

INSERT INTO app.resource_grants
    (id, org_id, identity, scope_type, scope_id, capability, granted_by)
SELECT 'rgrant_46_' || md5(p.org_id || ':' || pm.project_id || ':' || pm.identity),
       p.org_id, pm.identity, 'project', pm.project_id,
       CASE pm.role WHEN 'owner' THEN 'manage' WHEN 'member' THEN 'edit' ELSE 'view' END,
       pm.identity
FROM app.project_members pm
JOIN app.projects p ON p.id = pm.project_id
WHERE p.org_id IS NOT NULL
ON CONFLICT (org_id, identity, scope_type, scope_id) DO NOTHING;

DROP TABLE IF EXISTS app.project_members;

ALTER TABLE app.org_members ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0);

CREATE TABLE IF NOT EXISTS app.project_grant_changes (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    org_id              TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    identity            TEXT NOT NULL,
    membership_version  INTEGER NOT NULL CHECK (membership_version > 0),
    before_capability   TEXT CHECK (before_capability IN ('view', 'edit', 'manage')),
    after_capability    TEXT CHECK (after_capability IN ('view', 'edit', 'manage')),
    actor_identity      TEXT NOT NULL,
    state               TEXT NOT NULL DEFAULT 'prepared'
                        CHECK (state IN ('prepared', 'confirmed', 'expired', 'cancelled')),
    expires_at          TIMESTAMPTZ NOT NULL,
    idempotency_key     TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmed_at        TIMESTAMPTZ,
    UNIQUE (project_id, actor_identity, idempotency_key)
);

CREATE TABLE IF NOT EXISTS app.project_access_handoffs (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    org_id          TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    identity        TEXT,
    state           TEXT NOT NULL CHECK (state IN ('pending', 'delivered', 'accepted', 'expired', 'revoked', 'failed')),
    resume_ref      TEXT NOT NULL,
    expires_at      TIMESTAMPTZ NOT NULL,
    created_by      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS project_access_handoffs_pending
    ON app.project_access_handoffs (project_id, expires_at)
    WHERE state IN ('pending', 'delivered');

ALTER TABLE app.setup_journeys ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
ALTER TABLE app.setup_tasks ADD COLUMN IF NOT EXISTS authoritative_owner_ref JSONB;
ALTER TABLE app.setup_tasks ADD COLUMN IF NOT EXISTS readiness_ref JSONB;
ALTER TABLE app.setup_tasks ADD COLUMN IF NOT EXISTS resume_ref TEXT;
ALTER TABLE app.setup_tasks DROP CONSTRAINT IF EXISTS setup_tasks_owner_ref_object;
ALTER TABLE app.setup_tasks ADD CONSTRAINT setup_tasks_owner_ref_object
    CHECK (authoritative_owner_ref IS NULL OR jsonb_typeof(authoritative_owner_ref) = 'object');
ALTER TABLE app.setup_tasks DROP CONSTRAINT IF EXISTS setup_tasks_readiness_ref_object;
ALTER TABLE app.setup_tasks ADD CONSTRAINT setup_tasks_readiness_ref_object
    CHECK (readiness_ref IS NULL OR jsonb_typeof(readiness_ref) = 'object');
CREATE UNIQUE INDEX IF NOT EXISTS setup_journeys_one_active_project
    ON app.setup_journeys (project_id)
    WHERE project_id IS NOT NULL AND state = 'active';

CREATE TABLE IF NOT EXISTS app.setup_task_events (
    id              TEXT PRIMARY KEY,
    journey_id      TEXT NOT NULL REFERENCES app.setup_journeys(id) ON DELETE RESTRICT,
    task_id         TEXT REFERENCES app.setup_tasks(id) ON DELETE RESTRICT,
    event_type      TEXT NOT NULL CHECK (event_type IN (
                        'created', 'owner_changed', 'handoff_issued', 'handoff_revoked',
                        'handoff_expired', 'reconciled', 'completed', 'reopened'
                    )),
    actor_identity  TEXT NOT NULL,
    reason          TEXT NOT NULL,
    safe_refs       JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(safe_refs) = 'object'),
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS setup_task_events_journey_time
    ON app.setup_task_events (journey_id, occurred_at DESC, id DESC);

ALTER TABLE app.entry_confirmations DROP CONSTRAINT IF EXISTS entry_confirmations_command_type_check;
ALTER TABLE app.entry_confirmations ADD CONSTRAINT entry_confirmations_command_type_check
    CHECK (command_type IN (
        'hosted.entry_scope.create', 'instance.claim', 'project.settings.activate',
        'project.access.change'
    ));

COMMIT;
