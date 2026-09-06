-- Story 50.7: one frozen Render behind one revocable, expiring, audited Share.
--
-- WHY THIS EXISTS. Public sharing in this repository is currently three separate
-- mutable grants, and every one of them leaks the bearer into places nobody can
-- redact after the fact:
--
--   * `app.notebooks.share_token` (migration 016) -- a PLAINTEXT token, never
--     expiring, on a row whose public endpoint serves the LATEST successful run.
--     What the recipient sees changes under them, and the sender cannot know what
--     they saw.
--   * `app.render_snapshot_shares` (migration 054) -- a PLAINTEXT token with no
--     `project_id`, NO `expires_at` COLUMN AT ALL, and `ON DELETE CASCADE` onto a
--     snapshot the retention purge deletes. So the evidence that a grant existed
--     and was revoked can be destroyed by a background job.
--   * Both are read through `GET .../shared/{token}`: the bearer is in the URL
--     PATH, which puts it in the browser history, the referrer, every proxy log
--     and the ASGI access log -- before a single line of application code runs.
--     No handler-level redaction can reach that.
--
-- WHAT REPLACES THEM, and it is one object, not three: `app.render_shares`, a
-- Project-scoped grant to exactly ONE immutable Render (`app.renders`, migration
-- 154), with a mandatory expiry, an HMAC-only bearer, single-use consumption and
-- an append-only access log. AD-20 O1 (`ARCHITECTURE-SPINE.md:155-161`) and AD-30
-- (`:227-231`).
--
-- THE FOUR TABLES, and why each is separate rather than four more columns:
--
--     app.render_shares                     the GRANT      (mutable lifecycle)
--     app.render_share_exchange_sessions    the SESSION    (short-lived)
--     app.render_share_access_events        the EVIDENCE   (append-only)
--     app.render_share_feedback             the FEEDBACK   (append-only)
--
-- A grant advances state; evidence never does. Putting an append-only log in the
-- same table as a mutable lifecycle means one trigger has to distinguish them by
-- column, which is how an "append-only" table quietly becomes updatable.
--
-- WHAT IS DELIBERATELY *NOT* DONE HERE. Neither legacy table is dropped and
-- `app.notebooks.share_token` is not removed. Dropping them would erase the only
-- proof that the open grants were closed, and would remove the loud database-level
-- failure a remounted route must hit. The legacy rows are REVOKED, the column is
-- NULLED and CHECK-constrained, and an insert trigger refuses new plaintext
-- grants. CLAUDE.md anti-drift rule 3: destroying the trace of a retired path is
-- how the next reader rebuilds it.
--
-- THE ROLE IS A CLUSTER PREREQUISITE, NOT A SCHEMA OBJECT. `toorow_share_reader`
-- must already exist when this migration runs, and this file RAISES if it does
-- not. That is deliberate: the application role (`connector`) has neither
-- SUPERUSER nor CREATEROLE (verified 2026-07-31:
-- `select rolcreaterole from pg_roles where rolname = current_user` -> false), so a
-- `CREATE ROLE` here could only ever be wrapped in an exception handler -- and a
-- handler that swallows `insufficient_privilege` produces a migration that reports
-- success while the third enforcement layer of AC6 silently does not exist.
-- Failing loudly with the exact bootstrap statement beats passing quietly.

BEGIN;

-- ---------------------------------------------------------------------------
-- 0. The cluster prerequisite, asserted rather than assumed.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'toorow_share_reader') THEN
        RAISE EXCEPTION
            'Story 50.7: role toorow_share_reader is missing. It is a CLUSTER '
            'prerequisite, not a schema object, because the application role has '
            'no CREATEROLE. Run once as a superuser: '
            'CREATE ROLE toorow_share_reader NOLOGIN; '
            'GRANT toorow_share_reader TO %I;', current_user;
    END IF;
    IF NOT pg_has_role(current_user, 'toorow_share_reader', 'MEMBER') THEN
        RAISE EXCEPTION
            'Story 50.7: %I is not a member of toorow_share_reader, so '
            'SET LOCAL ROLE toorow_share_reader would fail at runtime. Run once as '
            'a superuser: GRANT toorow_share_reader TO %I;', current_user, current_user;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 1. The grant.
--
--    ONE Render, named once, as a Project-scoped COMPOSITE foreign key. There is
--    no `notebook_id`, no `notebook_run_id`, no `report_id`, no `result_id`, no
--    `query_spec_id` and no `snapshot_id` column anywhere in this table, and that
--    absence is the schema saying a Share cannot follow anything. A test asserts
--    the column list literally, because "we did not add one" is a convention and
--    this is meant to be a constraint.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.render_shares (
    id                        TEXT PRIMARY KEY,
    org_id                    TEXT NOT NULL,
    project_id                TEXT NOT NULL,
    render_id                 TEXT NOT NULL,

    -- The bearer NEVER lands here. Only hmac(pepper, "render-share-bearer:"||b).
    bearer_hash               TEXT NOT NULL,

    state                     TEXT NOT NULL DEFAULT 'active',

    -- AD-20: "revocable, EXPIRING and audited". The legacy table has no such
    -- column, which is precisely why every legacy link is unexpiring today.
    expires_at                TIMESTAMPTZ NOT NULL,

    created_by                TEXT NOT NULL,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    revoked_at                TIMESTAMPTZ,
    revoked_by                TEXT,
    revoke_reason_code        TEXT,

    -- Single use (Story 50.7 D4; the epic acceptance says "exchanged once").
    -- Non-null means the BEARER is dead. It does not mean the SHARE is: the
    -- session minted by that exchange keeps working until revocation or expiry.
    exchanged_at              TIMESTAMPTZ,

    exchange_attempt_count    INTEGER NOT NULL DEFAULT 0,
    exchange_attempt_ceiling  INTEGER NOT NULL DEFAULT 20,

    created_operation_id      TEXT NOT NULL,
    revoked_operation_id      TEXT,

    CONSTRAINT uq_render_shares_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT uq_render_shares_bearer_hash UNIQUE (bearer_hash),

    CONSTRAINT fk_render_shares_project
        FOREIGN KEY (org_id, project_id) REFERENCES app.projects (org_id, id),

    -- RESTRICT, not CASCADE. The legacy `054_render_snapshot_shares.sql:60-65`
    -- CASCADEs, and combined with the retention purge that means a revocation
    -- record can be deleted by a background job. An access grant's history must
    -- outlive the artifact it granted.
    CONSTRAINT fk_render_shares_render
        FOREIGN KEY (render_id, org_id, project_id)
        REFERENCES app.renders (id, org_id, project_id)
        ON DELETE RESTRICT,

    CONSTRAINT ck_render_shares_state
        CHECK (state IN ('active', 'revoked', 'expired')),
    CONSTRAINT ck_render_shares_expiry_after_creation
        CHECK (expires_at > created_at),
    CONSTRAINT ck_render_shares_bearer_hash_shape
        CHECK (bearer_hash ~ '^[0-9a-f]{64}$'),
    -- `revoked` and the revocation facts arrive together or not at all. A row
    -- claiming state='revoked' with no timestamp is a lie the schema can refuse.
    CONSTRAINT ck_render_shares_revocation_complete CHECK (
        (state = 'revoked') = (revoked_at IS NOT NULL)
        AND (revoked_at IS NULL) = (revoked_by IS NULL)
        AND (revoked_at IS NULL) = (revoke_reason_code IS NULL)
    ),
    CONSTRAINT ck_render_shares_ceiling_bounded
        CHECK (exchange_attempt_ceiling BETWEEN 1 AND 1000),
    CONSTRAINT ck_render_shares_attempts_nonnegative
        CHECK (exchange_attempt_count >= 0)
);

COMMENT ON TABLE app.render_shares IS
    'Story 50.7: one revocable, expiring, audited grant to exactly ONE immutable '
    'Render. It owns grant identity, Render identity, access lifecycle and audit '
    'evidence -- never data, never presentation state, and never a pointer to '
    '"latest". The absence of any notebook/report/result/query-spec column is the '
    'constraint that makes "sharing never follows the latest run" structural.';

COMMENT ON COLUMN app.render_shares.bearer_hash IS
    'hmac(TOOROW_RENDER_SHARE_PEPPER, "render-share-bearer:" || bearer, sha256). '
    'A database dump therefore contains nothing from which a live link can be '
    'rebuilt.';

COMMENT ON COLUMN app.render_shares.exchanged_at IS
    'Single-use consumption (D4). Set exactly once, under FOR UPDATE, by an '
    'UPDATE whose WHERE carries "AND exchanged_at IS NULL" and which aborts on '
    'rowcount <> 1. Kills the BEARER, not the Share.';

CREATE INDEX IF NOT EXISTS ix_render_shares_render
    ON app.render_shares (org_id, project_id, render_id);
CREATE INDEX IF NOT EXISTS ix_render_shares_state
    ON app.render_shares (org_id, project_id, state, expires_at);

-- ---------------------------------------------------------------------------
-- 2. The exchange session.
--
--    It carries `share_id` and the Project scope that composite FK needs -- and
--    nothing a request could name. No public handler reads a project, render,
--    result or organization identifier from the caller; every identity on the
--    public surface is derived from this row. That is AC6 layer 1, and it is a
--    property of the COLUMN LIST, not of handler discipline.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.render_share_exchange_sessions (
    id            TEXT PRIMARY KEY,
    share_id      TEXT NOT NULL,
    org_id        TEXT NOT NULL,
    project_id    TEXT NOT NULL,
    session_hash  TEXT NOT NULL,
    expires_at    TIMESTAMPTZ NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_render_share_sessions_hash UNIQUE (session_hash),
    CONSTRAINT fk_render_share_sessions_share
        FOREIGN KEY (share_id, org_id, project_id)
        REFERENCES app.render_shares (id, org_id, project_id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_render_share_sessions_hash_shape
        CHECK (session_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_render_share_sessions_expiry
        CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS ix_render_share_sessions_share
    ON app.render_share_exchange_sessions (share_id);

-- ---------------------------------------------------------------------------
-- 3. The access evidence. Append-only, and it never carries bearer material.
--
--    `share_id` IS NULLABLE, and exactly one reason code may use that: an
--    UNKNOWN bearer resolves to no Share at all, and AC5 requires the unknown
--    path to execute the SAME statements as the three known-Share refusals --
--    the lookup plus one appended event. A NOT NULL share_id would make the
--    unknown path append nothing, and the statement counts would differ, which
--    is exactly the enumeration oracle the design is built to avoid.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.render_share_access_events (
    id             TEXT PRIMARY KEY,
    share_id       TEXT,
    org_id         TEXT,
    project_id     TEXT,
    event          TEXT NOT NULL,
    outcome        TEXT NOT NULL,
    reason_code    TEXT NOT NULL,
    client_ip_hash TEXT,
    client_class   TEXT NOT NULL,
    occurred_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_render_share_events_share
        FOREIGN KEY (share_id, org_id, project_id)
        REFERENCES app.render_shares (id, org_id, project_id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_render_share_events_event CHECK (
        event IN ('exchanged', 'read', 'rows', 'exported', 'feedback',
                  'denied', 'expired', 'revoked', 'rate_limited')
    ),
    CONSTRAINT ck_render_share_events_outcome
        CHECK (outcome IN ('granted', 'refused')),
    -- MATCH SIMPLE lets a partially-NULL composite key skip the FK entirely.
    -- Spell out that the three scope columns are all-or-nothing, so a row cannot
    -- claim a project without a share and escape referential integrity.
    CONSTRAINT ck_render_share_events_scope_all_or_nothing CHECK (
        (share_id IS NULL AND org_id IS NULL AND project_id IS NULL)
        OR (share_id IS NOT NULL AND org_id IS NOT NULL AND project_id IS NOT NULL)
    ),
    -- The single licence to have no Share. Any other reason code must resolve one.
    CONSTRAINT ck_render_share_events_unresolved_is_unknown_bearer CHECK (
        (share_id IS NULL) = (reason_code = 'bearer_unknown')
    ),
    CONSTRAINT ck_render_share_events_client_class
        CHECK (client_class IN ('browser', 'non_browser', 'unknown')),
    CONSTRAINT ck_render_share_events_ip_hash_shape
        CHECK (client_ip_hash IS NULL OR client_ip_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_render_share_events_reason_bounded
        CHECK (char_length(reason_code) BETWEEN 1 AND 64)
);

COMMENT ON TABLE app.render_share_access_events IS
    'Story 50.7 AC9: one append-only row per exchange, read, export, feedback, '
    'denial, expiry-refusal, revoked-refusal and rate-limited attempt. It carries '
    'a peppered HMAC of the client IP and NO bearer, no bearer prefix, no session '
    'value and no session prefix. The legacy path persisted token[:8] into the '
    'audit spine (rendus_api.py:817-821); that is the defect this replaces.';

CREATE INDEX IF NOT EXISTS ix_render_share_events_share
    ON app.render_share_access_events (share_id, occurred_at DESC);

-- ---------------------------------------------------------------------------
-- 4. Feedback, with every version identity pinned NOT NULL.
--
--    `app.feedback` (migration 012) is not reused: it has `created_by TEXT NOT
--    NULL` -- a public recipient has no identity and inventing one would be a
--    fabricated actor -- and a `report_ref TEXT` where twelve exact identities are
--    required. Epic 51 reads these rows; this story only writes them.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.render_share_feedback (
    id                            TEXT PRIMARY KEY,
    share_id                      TEXT NOT NULL,
    org_id                        TEXT NOT NULL,
    project_id                    TEXT NOT NULL,
    render_id                     TEXT NOT NULL,
    result_id                     TEXT NOT NULL,
    visualization_spec_version_id TEXT NOT NULL,
    renderer_build                TEXT NOT NULL,
    runtime_build                 TEXT NOT NULL,
    theme_version                 TEXT NOT NULL,
    formatter_version             TEXT NOT NULL,
    responsive_profile            TEXT NOT NULL,
    evidence_manifest_hash        TEXT NOT NULL,
    selected_datum_key            TEXT,
    polarity                      TEXT NOT NULL,
    comment                       TEXT,
    submitted_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_render_share_feedback_share
        FOREIGN KEY (share_id, org_id, project_id)
        REFERENCES app.render_shares (id, org_id, project_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_render_share_feedback_render
        FOREIGN KEY (render_id, org_id, project_id)
        REFERENCES app.renders (id, org_id, project_id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_render_share_feedback_polarity
        CHECK (polarity IN ('helpful', 'not_helpful')),
    CONSTRAINT ck_render_share_feedback_profile
        CHECK (responsive_profile = 'share'),
    -- AD-31: the comment is untrusted text. Bounded here so the bound is not a
    -- handler's promise, and stored verbatim -- never interpolated into a prompt,
    -- a tool description or HTML.
    CONSTRAINT ck_render_share_feedback_comment_bounded
        CHECK (comment IS NULL OR char_length(comment) BETWEEN 1 AND 2000),
    -- Every pin must be an EXACT identity. `app.is_exact_pin` (migration 154)
    -- refuses 'legacy', 'current', 'deferred', 'latest', 'unknown' and 'none',
    -- so a feedback row cannot pin the thing it is supposed to distinguish.
    CONSTRAINT ck_render_share_feedback_pins_exact CHECK (
        app.is_exact_pin(visualization_spec_version_id)
        AND app.is_exact_pin(renderer_build)
        AND app.is_exact_pin(runtime_build)
        AND app.is_exact_pin(theme_version)
        AND app.is_exact_pin(formatter_version)
        AND app.is_exact_pin(evidence_manifest_hash)
    )
);

CREATE INDEX IF NOT EXISTS ix_render_share_feedback_render
    ON app.render_share_feedback (org_id, project_id, render_id, submitted_at DESC);

-- ---------------------------------------------------------------------------
-- 5. Append-only, in the database rather than in a service.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.reject_render_share_evidence_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'Story 50.7: % is append-only; % is refused. Access evidence and '
        'recipient feedback are the proof a grant was used -- rewriting them '
        'would make the proof worthless.', TG_TABLE_NAME, TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_render_share_access_events_append_only
    ON app.render_share_access_events;
CREATE TRIGGER trg_render_share_access_events_append_only
    BEFORE UPDATE OR DELETE ON app.render_share_access_events
    FOR EACH ROW EXECUTE FUNCTION app.reject_render_share_evidence_mutation();

DROP TRIGGER IF EXISTS trg_render_share_feedback_append_only
    ON app.render_share_feedback;
CREATE TRIGGER trg_render_share_feedback_append_only
    BEFORE UPDATE OR DELETE ON app.render_share_feedback
    FOR EACH ROW EXECUTE FUNCTION app.reject_render_share_evidence_mutation();

-- A Share's IDENTITY is immutable; only its lifecycle may advance. Re-pointing a
-- Share at another Render, or re-hashing its bearer, would silently change what a
-- delivered link opens -- for a recipient who has no way to notice.
CREATE OR REPLACE FUNCTION app.reject_render_share_identity_rewrite()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.org_id IS DISTINCT FROM OLD.org_id
       OR NEW.project_id IS DISTINCT FROM OLD.project_id
       OR NEW.render_id IS DISTINCT FROM OLD.render_id
       OR NEW.bearer_hash IS DISTINCT FROM OLD.bearer_hash
       OR NEW.created_by IS DISTINCT FROM OLD.created_by
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.created_operation_id IS DISTINCT FROM OLD.created_operation_id
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN
        RAISE EXCEPTION
            'Story 50.7: a Share''s identity, its Render, its bearer hash and its '
            'expiry are fixed at creation. Only state, revocation facts, '
            'exchanged_at and the attempt counter may advance.';
    END IF;
    IF OLD.exchanged_at IS NOT NULL AND NEW.exchanged_at IS DISTINCT FROM OLD.exchanged_at THEN
        RAISE EXCEPTION 'Story 50.7: a consumed bearer cannot be un-consumed or re-consumed.';
    END IF;
    IF OLD.state = 'revoked' AND NEW.state <> 'revoked' THEN
        RAISE EXCEPTION 'Story 50.7: a revoked Share never returns to another state.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_render_shares_identity_immutable ON app.render_shares;
CREATE TRIGGER trg_render_shares_identity_immutable
    BEFORE UPDATE ON app.render_shares
    FOR EACH ROW EXECUTE FUNCTION app.reject_render_share_identity_rewrite();

-- A Share row is never deleted (AC8): revocation keeps its full history.
CREATE OR REPLACE FUNCTION app.reject_render_share_delete()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'Story 50.7: a Share is revoked, never deleted. Deleting it would erase '
        'the only proof that a public grant existed and was closed.';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_render_shares_no_delete ON app.render_shares;
CREATE TRIGGER trg_render_shares_no_delete
    BEFORE DELETE ON app.render_shares
    FOR EACH ROW EXECUTE FUNCTION app.reject_render_share_delete();

CREATE OR REPLACE FUNCTION app.reject_render_share_truncate()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Story 50.7: % may not be truncated.', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_render_shares_block_truncate ON app.render_shares;
CREATE TRIGGER trg_render_shares_block_truncate
    BEFORE TRUNCATE ON app.render_shares
    EXECUTE FUNCTION app.reject_render_share_truncate();

DROP TRIGGER IF EXISTS trg_render_share_events_block_truncate
    ON app.render_share_access_events;
CREATE TRIGGER trg_render_share_events_block_truncate
    BEFORE TRUNCATE ON app.render_share_access_events
    EXECUTE FUNCTION app.reject_render_share_truncate();

DROP TRIGGER IF EXISTS trg_render_share_feedback_block_truncate
    ON app.render_share_feedback;
CREATE TRIGGER trg_render_share_feedback_block_truncate
    BEFORE TRUNCATE ON app.render_share_feedback
    EXECUTE FUNCTION app.reject_render_share_truncate();

-- ---------------------------------------------------------------------------
-- 6. RLS, the house shape (migration 151).
-- ---------------------------------------------------------------------------
ALTER TABLE app.render_shares ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.render_shares FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS render_shares_strict ON app.render_shares;
CREATE POLICY render_shares_strict ON app.render_shares
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    );

ALTER TABLE app.render_share_exchange_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.render_share_exchange_sessions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS render_share_sessions_strict ON app.render_share_exchange_sessions;
CREATE POLICY render_share_sessions_strict ON app.render_share_exchange_sessions
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    );

-- The access log is scope-nullable by design (an unknown bearer resolves no
-- Share), so the policy must admit the unresolved row rather than silently drop
-- it -- a refusal that cannot be logged is a refusal nobody can audit.
ALTER TABLE app.render_share_access_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.render_share_access_events FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS render_share_events_strict ON app.render_share_access_events;
CREATE POLICY render_share_events_strict ON app.render_share_access_events
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR org_id IS NULL
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR org_id IS NULL
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    );

ALTER TABLE app.render_share_feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.render_share_feedback FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS render_share_feedback_strict ON app.render_share_feedback;
CREATE POLICY render_share_feedback_strict ON app.render_share_feedback
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    );

-- ---------------------------------------------------------------------------
-- 7. The public database role: AC6 layer 2.
--
--    Enumerated as GRANTs on exactly seven objects and NOTHING else. There is no
--    `GRANT ... ON ALL TABLES IN SCHEMA app`, and there must never be: the whole
--    point is that a public handler which asks for `app.query_results` is refused
--    by PostgreSQL with 42501, not by a handler remembering not to ask.
--
--    `snapshot_shares.py:231-234` promises the same restriction TODAY -- in a
--    docstring. A comment is not enforcement.
-- ---------------------------------------------------------------------------
GRANT USAGE ON SCHEMA app TO toorow_share_reader;

GRANT SELECT ON app.render_shares                      TO toorow_share_reader;
GRANT SELECT ON app.render_share_exchange_sessions     TO toorow_share_reader;
GRANT SELECT ON app.renders                            TO toorow_share_reader;
GRANT SELECT, INSERT ON app.render_share_access_events TO toorow_share_reader;
GRANT INSERT ON app.render_share_feedback              TO toorow_share_reader;

-- The exchange is the one write on a Share the public path performs: consuming
-- the bearer, counting a failed attempt, and self-revoking at the ceiling. It is
-- column-scoped so the role cannot move a Share to another Render, extend its
-- expiry or un-revoke it even if a handler tried.
GRANT UPDATE (exchanged_at, exchange_attempt_count, state,
              revoked_at, revoked_by, revoke_reason_code)
    ON app.render_shares TO toorow_share_reader;
GRANT INSERT ON app.render_share_exchange_sessions TO toorow_share_reader;

-- Belt and braces: revoke anything a future `GRANT ... ON ALL TABLES` might have
-- handed this role on the tables the public path must never reach. Listed by
-- name, so a reader can diff this block against AC6.2 without running anything.
REVOKE ALL ON app.query_results             FROM toorow_share_reader;
REVOKE ALL ON app.query_result_payloads     FROM toorow_share_reader;
REVOKE ALL ON app.query_specs               FROM toorow_share_reader;
REVOKE ALL ON app.query_spec_versions       FROM toorow_share_reader;
REVOKE ALL ON app.query_execution_attempts  FROM toorow_share_reader;
REVOKE ALL ON app.projects                  FROM toorow_share_reader;
REVOKE ALL ON app.organizations             FROM toorow_share_reader;
REVOKE ALL ON app.notebooks                 FROM toorow_share_reader;
REVOKE ALL ON app.render_snapshots          FROM toorow_share_reader;
REVOKE ALL ON app.render_snapshot_shares    FROM toorow_share_reader;

-- The role also has to pass `app.renders`'s own RLS. It does, without a second
-- policy: the migration-154 predicate admits any transaction that has not armed
-- `toorow.enforce_epic36`, and the public connection deliberately arms no Epic-36
-- identity because a recipient HAS no identity. Isolation on this path comes from
-- the grant list above plus the fact that every identity is derived from
-- `share_id` -- not from an access context there is nobody to fill in.

-- ---------------------------------------------------------------------------
-- 8. THE RETIREMENT WRITES (AC10). Counts are emitted, not assumed.
--
--    Every row closed here was, until this statement ran, an unexpiring,
--    unauthenticated, publicly-readable grant to Project data.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    open_snapshot_shares INTEGER;
    open_notebook_tokens INTEGER;
BEGIN
    UPDATE app.render_snapshot_shares
       SET revoked_at = NOW()
     WHERE revoked_at IS NULL;
    GET DIAGNOSTICS open_snapshot_shares = ROW_COUNT;

    UPDATE app.notebooks
       SET share_token = NULL
     WHERE share_token IS NOT NULL;
    GET DIAGNOSTICS open_notebook_tokens = ROW_COUNT;

    RAISE NOTICE
        'Story 50.7 retirement: revoked % open app.render_snapshot_shares row(s); '
        'nulled % app.notebooks.share_token value(s). Each was an unexpiring, '
        'unauthenticated public grant before this statement.',
        open_snapshot_shares, open_notebook_tokens;
    -- No synthetic access-event row is written for these. `render_share_access_events`
    -- records what a RECIPIENT did to an `app.render_shares` grant; the legacy rows
    -- closed above belong to no such grant, and manufacturing an event for them
    -- would put a fabricated actor into the evidence table Epic 51 reads. The
    -- NOTICE above is the migration's audit output, and the closed rows keep their
    -- own `revoked_at` as the durable proof.
END $$;

-- No new plaintext grant, ever. The trigger is what a remounted legacy route
-- hits: loud, at the database, rather than a silently successful INSERT.
CREATE OR REPLACE FUNCTION app.reject_render_snapshot_share_insert()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'Story 50.7: app.render_snapshot_shares is RETIRED. It stored a plaintext '
        'bearer, had no project scope and no expiry, and CASCADE-deleted with a '
        'purgeable snapshot. Create an app.render_shares grant over one immutable '
        'app.renders row instead (POST the Render Workbench Sharing tab). The '
        'existing rows and the revocation history are kept deliberately.';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_render_snapshot_shares_retired ON app.render_snapshot_shares;
CREATE TRIGGER trg_render_snapshot_shares_retired
    BEFORE INSERT ON app.render_snapshot_shares
    FOR EACH ROW EXECUTE FUNCTION app.reject_render_snapshot_share_insert();

COMMENT ON TABLE app.render_snapshot_shares IS
    'RETIRED by Story 50.7 (migration 162). Kept, not dropped: the rows are the '
    'only proof the open grants were closed, and the insert trigger is the loud '
    'failure a remounted route must hit. Superseded by app.render_shares.';

-- `app.notebooks.share_token` follows the same discipline: nulled, then refused
-- by a CHECK. The column stays as the trace of what was retired.
ALTER TABLE app.notebooks
    DROP CONSTRAINT IF EXISTS ck_notebooks_share_token_retired;
ALTER TABLE app.notebooks
    ADD CONSTRAINT ck_notebooks_share_token_retired CHECK (share_token IS NULL);

COMMENT ON COLUMN app.notebooks.share_token IS
    'RETIRED by Story 50.7. Held a PLAINTEXT, never-expiring bearer whose public '
    'endpoint served the LATEST successful run, so what a recipient saw changed '
    'under them. Values nulled and CHECK-constrained to NULL; the column is kept '
    'as the trace of the retirement. Superseded by app.render_shares.';

COMMIT;
