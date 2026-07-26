-- 109_entry_invitation_without_org.sql
--
-- ONE invitation object, with or without a target organization (decision Jean,
-- 2026-07-25 -- stated three times, not to be reinterpreted):
--
--   * WITHOUT org (`org_id IS NULL`) -- the ENTRY invitation, issued by a PLATFORM
--     admin after a waitlist approval. Accepting it creates NO membership: the
--     person lands on "Welcome to toorow -> Create your organization" and creates
--     THEIR OWN. Zero org means zero org: no ghost organization, no placeholder,
--     no organization pre-created server-side.
--   * WITH org -- unchanged: the person joins THAT organization, with the role
--     recorded in the invitation.
--
-- 062 declared `app.invitations.org_id TEXT NOT NULL REFERENCES app.organizations`,
-- so the whole chain assumed an organization everywhere. This migration removes
-- that assumption -- and only that assumption.
--
-- WHAT ELSE THIS TOUCHES, AND WHY
-- -------------------------------
-- 1. `app.operations.effective_org_id` (060) is `NOT NULL REFERENCES
--    app.organizations`. Issuing and accepting an invitation both go through
--    `execute_operation`, and `app.invitations.operation_id` is `NOT NULL` -- so
--    an entry invitation is IMPOSSIBLE while an operation requires an org. It is
--    made nullable here. NULL means "platform scope": an act of the platform on
--    itself, belonging to no tenant. This is NOT a new org and NOT a sentinel row.
--
--    Consequence on idempotency: `uq_operations_idempotency` is
--    UNIQUE (effective_org_id, command_type, idempotency_key_hash), and in Postgres
--    a UNIQUE constraint never collides on NULL -- platform-scope operations would
--    silently lose their replay protection. A PARTIAL unique index restores it for
--    exactly those rows. `core/operations.py` matches this with an IS NOT DISTINCT
--    FROM lookup and an untargeted ON CONFLICT DO NOTHING (which arbitrates on both
--    indexes, not just the named constraint).
--
-- 2. The immutability trigger (062, hardened by 066) compares
--    `NEW.org_id IS DISTINCT FROM OLD.org_id`. NULL IS DISTINCT FROM NULL is FALSE,
--    so an invitation whose org_id is NULL on both sides passes -- and an UPDATE
--    that tried to attach an org to an entry invitation would still be rejected.
--    Exactly the wanted behaviour: NOTHING to change there.
--
-- 3. The listing index `invitations_org_created (org_id, created_at DESC)` is a
--    btree, and btrees DO index NULLs: `WHERE org_id IS NULL ORDER BY created_at
--    DESC` still uses it. No entry invitation disappears from a read for want of an
--    index. `list_safe_invitations` filters with IS NOT DISTINCT FROM so the same
--    query serves both scopes. There is no RLS policy on app.invitations (checked:
--    the only migration enabling RLS is 059, on another table), so no policy can
--    hide a NULL-org row either.
--
-- 4. `app.setup_journeys.org_id` (065) stays NOT NULL, deliberately. A setup
--    journey is the checklist of an ORGANIZATION's first datastream; a person who
--    has not created their organization yet has no journey to bootstrap. The
--    acceptance path skips it when there is no org, instead of inventing one.
--
-- 5. New CHECK: an invitation without an org carries NO grant bindings. Project
--    and datastream grants are org-scoped resources; there is nothing to grant
--    before the person has an organization. Structural, so no code path can write
--    an entry invitation that pretends to grant something.
--
-- Relaxation only -- no existing row can violate any of this (every current
-- invitation has an org, and grant_bindings is untouched for them).

BEGIN;

-- 1. The invitation itself: an organization is now OPTIONAL.
ALTER TABLE app.invitations ALTER COLUMN org_id DROP NOT NULL;

ALTER TABLE app.invitations
    DROP CONSTRAINT IF EXISTS ck_entry_invitation_owner_role;
ALTER TABLE app.invitations
    ADD CONSTRAINT ck_entry_invitation_owner_role
    CHECK (org_id IS NOT NULL OR invited_role = 'owner');

ALTER TABLE app.invitations
    DROP CONSTRAINT IF EXISTS ck_invitation_grants_require_org;
ALTER TABLE app.invitations
    ADD CONSTRAINT ck_invitation_grants_require_org
    CHECK (org_id IS NOT NULL OR grant_bindings = '[]'::jsonb);

COMMENT ON COLUMN app.invitations.org_id IS
    'Target organization, NULLABLE since migration 109. NULL = ENTRY invitation '
    '(waitlist approval): accepting it creates NO membership, the person then '
    'creates their own organization. NOT NULL = the person joins THAT organization '
    'as a member with invited_role.';

-- 2. The operations envelope: platform scope = no organization at all.
ALTER TABLE app.operations ALTER COLUMN effective_org_id DROP NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS operations_platform_idempotency
    ON app.operations (command_type, idempotency_key_hash)
    WHERE effective_org_id IS NULL;

COMMENT ON COLUMN app.operations.effective_org_id IS
    'Organization the operation acts for. NULLABLE since migration 109: NULL means '
    'PLATFORM SCOPE -- an act of the platform that belongs to no tenant (issuing / '
    'accepting an entry invitation). Idempotency for those rows is carried by the '
    'partial unique index operations_platform_idempotency, because the UNIQUE '
    'constraint on (effective_org_id, command_type, idempotency_key_hash) never '
    'collides when the org is NULL.';

COMMIT;
