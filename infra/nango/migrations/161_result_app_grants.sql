-- Story 50.6 -- the scoped Result handle, stored as a row so it cannot be a credential.
--
-- WHAT THIS TABLE IS FOR. `docs/product-architecture/visualization-and-rendering.md`
-- ("Small and large Results") requires that a large Result reach a widget through a
-- scoped opaque handle plus allowlisted, bounded slice reads, and that "the handle is
-- neither a credential nor a warehouse query escape hatch".
--
-- WHY A ROW AND NOT A SIGNED TOKEN (D1). A token that carries its own scope IS a
-- credential: it authorizes by presentation, revocation degrades into a denylist, and
-- "recheck authorization on every read" quietly becomes "verify the signature". A row
-- makes the opposite mechanical -- `handle_id` decodes to nothing, every field that
-- narrows a read lives here, and `core.result_slices` resolves the CALLER's access
-- BEFORE it ever loads this row. The grant can only narrow what the access decision
-- already permitted; there is no column here that widens anything.
--
-- WHY A SEPARATE TABLE AND NOT COLUMNS ON app.query_results (D2). Migration 151 makes
-- `app.query_results` insert-once under `app.reject_analytical_evidence_mutation`.
-- Revocation requires an UPDATE, so grant columns there would have forced a hole in
-- the Result's immutability guard to store something that is not evidence.
--
-- IMMUTABILITY, BUT NOT THE 151 KIND. A grant is not evidence, so it is not
-- insert-once: `revoked_at` must be settable or revocation is a promise. The trigger
-- below therefore refuses any UPDATE that touches ANY column other than `revoked_at`.
-- Revocation is the only legitimate mutation, and it is the only one reachable.

-- ---------------------------------------------------------------------------
-- 1. The grant.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.result_app_grants (
    handle_id           TEXT PRIMARY KEY,
    org_id              TEXT NOT NULL,
    project_id          TEXT NOT NULL,
    result_id           TEXT NOT NULL,
    -- Pinned at issue time and verified on EVERY read. Immutability makes a
    -- mismatch unreachable, which is exactly the point: the assumption becomes
    -- checkable instead of assumed (AC6 property 6).
    content_hash        TEXT NOT NULL,
    issued_to_identity  TEXT NOT NULL,
    -- Derived at issue time from app.query_result_payloads.result_schema.fields[*].name.
    -- A read may ask for a subset; it can never widen this list.
    allowed_columns     TEXT[] NOT NULL,
    issued_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_read_at        TIMESTAMPTZ,
    expires_at          TIMESTAMPTZ NOT NULL,
    revoked_at          TIMESTAMPTZ,

    -- Project-scoped composite FK, the same shape migration 151 uses for
    -- app.query_result_payloads: a grant cannot point at another Project's Result
    -- even if the application forgot its own scope predicate.
    CONSTRAINT fk_result_app_grants_result
        FOREIGN KEY (result_id, org_id, project_id)
        REFERENCES app.query_results (id, org_id, project_id),
    -- `rh_` + Crockford base32 ULID. The prefix is a reading aid; the 26 characters
    -- after it are random and decode to nothing.
    CONSTRAINT ck_result_app_grants_handle_shape
        CHECK (handle_id ~ '^rh_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT ck_result_app_grants_hash
        CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_result_app_grants_expiry_after_issue
        CHECK (expires_at > issued_at),
    -- A grant that allows no column is not a narrower grant, it is a broken one:
    -- refuse it at issue time rather than let a read discover it.
    CONSTRAINT ck_result_app_grants_columns_present
        CHECK (cardinality(allowed_columns) > 0)
);

CREATE INDEX IF NOT EXISTS idx_result_app_grants_project_result
    ON app.result_app_grants (project_id, result_id);
CREATE INDEX IF NOT EXISTS idx_result_app_grants_expires
    ON app.result_app_grants (expires_at);

-- ---------------------------------------------------------------------------
-- 2. Revocation is the ONLY legitimate mutation.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.reject_result_grant_rewrite()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'a Result handle grant is revoked, never deleted'
            USING ERRCODE = '23000';
    END IF;

    IF NEW.handle_id          <> OLD.handle_id
       OR NEW.org_id          <> OLD.org_id
       OR NEW.project_id      <> OLD.project_id
       OR NEW.result_id       <> OLD.result_id
       OR NEW.content_hash    <> OLD.content_hash
       OR NEW.issued_to_identity <> OLD.issued_to_identity
       OR NEW.allowed_columns IS DISTINCT FROM OLD.allowed_columns
       OR NEW.issued_at       <> OLD.issued_at
       OR NEW.expires_at      <> OLD.expires_at
    THEN
        RAISE EXCEPTION
            'a Result handle grant may only be revoked or touched; its scope is fixed at issue'
            USING ERRCODE = '23000';
    END IF;

    -- Un-revoking would turn revocation into a suggestion.
    IF OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at THEN
        RAISE EXCEPTION 'a revoked Result handle grant cannot be reinstated'
            USING ERRCODE = '23000';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_result_app_grants_revoke_only ON app.result_app_grants;
CREATE TRIGGER trg_result_app_grants_revoke_only
BEFORE UPDATE OR DELETE ON app.result_app_grants
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_result_grant_rewrite();

-- ---------------------------------------------------------------------------
-- 3. Fail-closed Row Level Security -- the same six-policy shape as migration 151.
--
--    RLS enabled with no applicable policy exposes no rows; FORCE applies it to the
--    table owner too, so an isolation test cannot pass vacuously against a
--    superuser or owner connection. The application authorization check still runs
--    first: this is the floor, not the door.
-- ---------------------------------------------------------------------------
ALTER TABLE app.result_app_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.result_app_grants FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS result_app_grants_strict ON app.result_app_grants;
CREATE POLICY result_app_grants_strict ON app.result_app_grants
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    );
