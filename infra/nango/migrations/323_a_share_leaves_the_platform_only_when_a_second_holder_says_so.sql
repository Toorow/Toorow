-- 323: external sharing is a project-scoped capability, and the exit needs a
--      SECOND person.
--
-- WHY THIS EXISTS. `docs/product-architecture/proactive-assertions.md` decision 2
-- answers "may an unvalidated proactive claim be shared outside the platform?"
-- with "No -- the exit is where the human belongs. External sharing requires a
-- project-scoped capability plus confirmation by a *second* role holder", and it
-- names what was missing in the same paragraph: "What is missing is the
-- project-scope switch that lets a project forbid external sharing at all, and
-- the two-person confirmation."  Both halves were still missing on 2026-08-30:
--
--     grep -rcE 'external_sharing|share_policy|forbid_external|allow_external' \
--         server/core/render_shares.py server/core/render_shares_api.py
--     -> 0 and 0
--
--     render_shares.py:482  confirmation_mode="none"   on share CREATION
--     render_shares.py:560  confirmation_mode="server" on revocation
--     both with confirmation_reference=None
--
-- So a Render -- including one carrying a model-authored assertion -- left the
-- platform on one person's `edit` role, and no project could refuse.
--
-- ---------------------------------------------------------------------------
-- 1. THE SWITCH, AND WHY ITS DEFAULT IS `forbidden`
-- ---------------------------------------------------------------------------
--
-- It lives on `app.project_preferences` -- the store `project-settings.md` calls
-- the Project's "explicit effective defaults" -- and NOT on
-- `app.project_capabilities`.  The six rows of that table are compiled against
-- every applicable Datastream and carry coverage, dependencies and an impact
-- review; external sharing compiles into no Datastream and changes no figure. A
-- seventh capability key would owe a compiler that has nothing to compile.
--
-- The default is `forbidden`, for the reason `project-settings.md` already gives
-- for every optional capability -- "optional capabilities start Disabled unless
-- the operator confirms a proposal" -- and it applies with more force here,
-- because the act it governs is the one act of the product that cannot be
-- undone for the person who received the link.
--
-- EXISTING PROJECTS ARE BACKFILLED TO `forbidden` TOO, and that is deliberate
-- rather than an oversight of the `DEFAULT` clause: a project that shared before
-- this migration never authorized sharing, because there was nothing to
-- authorize with. Grandfathering them to `allowed` would make the switch
-- decorative on every project that exists today, which is the only population
-- there is.
--
-- ---------------------------------------------------------------------------
-- 2. THE CEREMONY, AND WHY THE BEARER IS MINTED LATE
-- ---------------------------------------------------------------------------
--
-- A Share is now born `pending_confirmation` WITH NO BEARER AT ALL. Migration
-- 162 made `bearer_hash` NOT NULL and immutable; this migration makes it
-- nullable and settable exactly once, from NULL, and forbids every other
-- rewrite exactly as before.
--
-- Minting at creation and refusing the exchange until confirmation would also
-- work, and is refused: it hands the requester a URL that is a live grant the
-- moment somebody else clicks Confirm, so the second holder would be authorizing
-- a link already in flight. With no bearer to hand out, the confirmation is the
-- act that CREATES the link, and the person who authorized the exit is the
-- person who holds it.
--
-- It also makes the guarantee structural rather than procedural: a pending Share
-- has no `bearer_hash`, so `exchange_bearer`'s UNIQUE lookup on that column can
-- never resolve to one. There is no branch to forget.
--
-- ---------------------------------------------------------------------------
-- 3. THE CONFIRMATION RECORD
-- ---------------------------------------------------------------------------
--
-- `app.render_share_confirmations` is the ticket, one per Share, consumed once.
-- It is NOT `app.entry_confirmations`: that ceremony binds issue and consumption
-- to the SAME `actor_person_id` (`core/entry_confirmations.py`), which is the
-- exact opposite of what decision 2 asks for. Here the schema itself refuses a
-- confirmation by the requester -- `ck_render_share_confirmations_second_holder`
-- -- so "a second role holder" is a constraint and not a code path.
--
-- The row is the operation's `confirmation_reference` on BOTH halves: the
-- `render_share.create` operation names the ticket it minted, and the
-- `render_share.confirm` operation names the ticket it consumed. One
-- `confirmation_reference_hash` therefore joins the request to its confirmation
-- in `app.audit_log`, with no new column anywhere.
--
-- ERASURE. The table hangs off `app.render_shares (id, org_id, project_id)` with
-- NO ACTION and NOT `ON DELETE CASCADE`, and that is deliberate:
-- `core.org_purge.plan_purge` walks `confdeltype IN ('a','r')` only, so a
-- cascade child is invisible to the plan and is erased by Postgres from a
-- statement `org_purge` never names. NO ACTION puts the table IN the plan, where
-- an erasure that fails on it fails by name. Its DELETE guard therefore carries
-- the `app.rgpd_erasure` WHEN clause of migrations 099/200/209 -- an append-only
-- guard that forgets it re-blocks an organization erasure, and this repository
-- has already repaired that twice. Its GRANT is written WITH its REVOKE, which
-- is migration 316's lesson: 207's `ALTER DEFAULT PRIVILEGES` hands `connector`
-- SELECT, INSERT, UPDATE, DELETE on every table created after it, so a narrow
-- `GRANT SELECT, INSERT, UPDATE` is declarative until the revoke lands.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. The project-scoped capability.
-- ---------------------------------------------------------------------------
ALTER TABLE app.project_preferences
    ADD COLUMN IF NOT EXISTS external_sharing TEXT NOT NULL DEFAULT 'forbidden';

ALTER TABLE app.project_preferences
    DROP CONSTRAINT IF EXISTS chk_project_preferences_external_sharing;
ALTER TABLE app.project_preferences
    ADD CONSTRAINT chk_project_preferences_external_sharing
    CHECK (external_sharing IN ('allowed', 'forbidden'));

-- Who decided, and when. NULL means nobody has: the platform default stands, and
-- the screen says that rather than attributing the refusal to a person.
ALTER TABLE app.project_preferences
    ADD COLUMN IF NOT EXISTS external_sharing_decided_by TEXT;
ALTER TABLE app.project_preferences
    ADD COLUMN IF NOT EXISTS external_sharing_decided_at TIMESTAMPTZ;

ALTER TABLE app.project_preferences
    DROP CONSTRAINT IF EXISTS chk_project_preferences_external_sharing_decision;
ALTER TABLE app.project_preferences
    ADD CONSTRAINT chk_project_preferences_external_sharing_decision
    CHECK ((external_sharing_decided_by IS NULL) = (external_sharing_decided_at IS NULL));

COMMENT ON COLUMN app.project_preferences.external_sharing IS
    'proactive-assertions.md decision 2: the project-scoped capability that lets '
    'a Project forbid external sharing at all. Default forbidden, backfilled '
    'forbidden -- a project that shared before migration 323 never authorized '
    'sharing, because there was nothing to authorize with.';

-- ---------------------------------------------------------------------------
-- 2. A Share may be pending, and a pending Share has no bearer.
-- ---------------------------------------------------------------------------
ALTER TABLE app.render_shares ALTER COLUMN bearer_hash DROP NOT NULL;

ALTER TABLE app.render_shares DROP CONSTRAINT IF EXISTS ck_render_shares_state;
ALTER TABLE app.render_shares
    ADD CONSTRAINT ck_render_shares_state
    CHECK (state IN ('pending_confirmation', 'active', 'revoked', 'expired'));

-- The two halves of "no link before a second person says so", as constraints:
-- a pending Share carries no bearer, and a live one always carries one. A
-- pending Share that is REVOKED before anyone confirmed keeps its NULL, which is
-- why this is two implications and not one equivalence.
ALTER TABLE app.render_shares
    DROP CONSTRAINT IF EXISTS ck_render_shares_pending_has_no_bearer;
ALTER TABLE app.render_shares
    ADD CONSTRAINT ck_render_shares_pending_has_no_bearer
    CHECK (state <> 'pending_confirmation' OR bearer_hash IS NULL);

ALTER TABLE app.render_shares
    DROP CONSTRAINT IF EXISTS ck_render_shares_active_has_a_bearer;
ALTER TABLE app.render_shares
    ADD CONSTRAINT ck_render_shares_active_has_a_bearer
    CHECK (state <> 'active' OR bearer_hash IS NOT NULL);

COMMENT ON COLUMN app.render_shares.bearer_hash IS
    'hmac(TOOROW_RENDER_SHARE_PEPPER, "render-share-bearer:" || bearer, sha256). '
    'A database dump therefore contains nothing from which a live link can be '
    'rebuilt. NULL until a SECOND role holder confirms the Share (migration '
    '323): the confirmation is the act that creates the link, so there is no '
    'window in which a URL exists that only needs somebody else to click.';

-- ---------------------------------------------------------------------------
-- 3. The confirmation ticket.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.render_share_confirmations (
    id                      TEXT PRIMARY KEY,
    share_id                TEXT NOT NULL,
    org_id                  TEXT NOT NULL,
    project_id              TEXT NOT NULL,

    requested_by            TEXT NOT NULL,
    requested_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    requested_operation_id  TEXT NOT NULL,
    expires_at              TIMESTAMPTZ NOT NULL,

    confirmed_by            TEXT,
    confirmed_at            TIMESTAMPTZ,
    confirmed_operation_id  TEXT,

    CONSTRAINT uq_render_share_confirmations_share UNIQUE (share_id),
    CONSTRAINT uq_render_share_confirmations_scope UNIQUE (id, org_id, project_id),

    -- NO ACTION, so `core.org_purge.plan_purge` -- which walks
    -- `confdeltype IN ('a','r')` -- can SEE this table and emit its own DELETE
    -- for it. A cascade child is erased by a statement the purge never names.
    CONSTRAINT fk_render_share_confirmations_share
        FOREIGN KEY (share_id, org_id, project_id)
        REFERENCES app.render_shares (id, org_id, project_id),

    CONSTRAINT ck_render_share_confirmations_window
        CHECK (expires_at > requested_at),

    -- The confirmation facts arrive together or not at all.
    CONSTRAINT ck_render_share_confirmations_complete CHECK (
        (confirmed_at IS NULL) = (confirmed_by IS NULL)
        AND (confirmed_at IS NULL) = (confirmed_operation_id IS NULL)
    ),

    -- DECISION 2, IN THE SCHEMA. The person who asked for the exit is not the
    -- person who may authorize it. A code path can be forgotten; this cannot.
    CONSTRAINT ck_render_share_confirmations_second_holder
        CHECK (confirmed_by IS NULL OR confirmed_by <> requested_by)
);

COMMENT ON TABLE app.render_share_confirmations IS
    'proactive-assertions.md decision 2: one Share, one ticket, consumed once by '
    'a SECOND role holder. It is the operation confirmation_reference of BOTH '
    'render_share.create and render_share.confirm, so one '
    'confirmation_reference_hash joins the request to its confirmation in '
    'app.audit_log.';

CREATE INDEX IF NOT EXISTS ix_render_share_confirmations_pending
    ON app.render_share_confirmations (org_id, project_id, expires_at)
    WHERE confirmed_at IS NULL;

-- ---------------------------------------------------------------------------
-- 4. The ticket is consumed once, and never rewritten.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.reject_render_share_confirmation_rewrite()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.share_id IS DISTINCT FROM OLD.share_id
       OR NEW.org_id IS DISTINCT FROM OLD.org_id
       OR NEW.project_id IS DISTINCT FROM OLD.project_id
       OR NEW.requested_by IS DISTINCT FROM OLD.requested_by
       OR NEW.requested_at IS DISTINCT FROM OLD.requested_at
       OR NEW.requested_operation_id IS DISTINCT FROM OLD.requested_operation_id
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN
        RAISE EXCEPTION
            'Migration 323: a Share confirmation ticket names one Share, one '
            'requester and one window, all fixed when the Share was requested.';
    END IF;
    IF OLD.confirmed_at IS NOT NULL THEN
        RAISE EXCEPTION
            'Migration 323: a Share confirmation is consumed exactly once. Revoke '
            'the Share and request a new one.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_render_share_confirmations_consume_once
    ON app.render_share_confirmations;
CREATE TRIGGER trg_render_share_confirmations_consume_once
    BEFORE UPDATE ON app.render_share_confirmations
    FOR EACH ROW EXECUTE FUNCTION app.reject_render_share_confirmation_rewrite();

CREATE OR REPLACE FUNCTION app.reject_render_share_confirmation_delete()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'Migration 323: a Share confirmation is kept, never deleted. Deleting it '
        'would erase the only proof that a second person authorized the exit.';
END;
$$ LANGUAGE plpgsql;

-- The WHEN clause is migration 099's erasure hatch. Without it, an organization
-- erasure that reaches app.render_shares is blocked by this table's cascade.
DROP TRIGGER IF EXISTS trg_render_share_confirmations_no_delete
    ON app.render_share_confirmations;
CREATE TRIGGER trg_render_share_confirmations_no_delete
    BEFORE DELETE ON app.render_share_confirmations
    FOR EACH ROW
    WHEN (COALESCE(current_setting('app.rgpd_erasure', true), 'off') <> 'on')
    EXECUTE FUNCTION app.reject_render_share_confirmation_delete();

DROP TRIGGER IF EXISTS trg_render_share_confirmations_block_truncate
    ON app.render_share_confirmations;
CREATE TRIGGER trg_render_share_confirmations_block_truncate
    BEFORE TRUNCATE ON app.render_share_confirmations
    EXECUTE FUNCTION app.reject_render_share_truncate();

-- ---------------------------------------------------------------------------
-- 5. The Share identity guard, widened by exactly one hole.
--
--    Migration 162 froze `bearer_hash` outright. It stays frozen once it holds a
--    value; what is now permitted is the single NULL -> value write the
--    confirmation performs. Everything else 162 refused is refused verbatim, and
--    a Share can never return to `pending_confirmation` -- a live link cannot be
--    put back in its envelope.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.reject_render_share_identity_rewrite()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.org_id IS DISTINCT FROM OLD.org_id
       OR NEW.project_id IS DISTINCT FROM OLD.project_id
       OR NEW.render_id IS DISTINCT FROM OLD.render_id
       OR NEW.created_by IS DISTINCT FROM OLD.created_by
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.created_operation_id IS DISTINCT FROM OLD.created_operation_id
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN
        RAISE EXCEPTION
            'Story 50.7: a Share''s identity, its Render, its bearer hash and its '
            'expiry are fixed at creation. Only state, revocation facts, '
            'exchanged_at and the attempt counter may advance.';
    END IF;
    IF OLD.bearer_hash IS NOT NULL
       AND NEW.bearer_hash IS DISTINCT FROM OLD.bearer_hash THEN
        RAISE EXCEPTION
            'Story 50.7: a Share''s bearer hash is fixed once it exists. '
            'Re-hashing it would silently change what a delivered link opens.';
    END IF;
    IF OLD.exchanged_at IS NOT NULL AND NEW.exchanged_at IS DISTINCT FROM OLD.exchanged_at THEN
        RAISE EXCEPTION 'Story 50.7: a consumed bearer cannot be un-consumed or re-consumed.';
    END IF;
    IF OLD.state = 'revoked' AND NEW.state <> 'revoked' THEN
        RAISE EXCEPTION 'Story 50.7: a revoked Share never returns to another state.';
    END IF;
    IF OLD.state <> 'pending_confirmation' AND NEW.state = 'pending_confirmation' THEN
        RAISE EXCEPTION
            'Migration 323: a Share that has already been confirmed cannot go back '
            'to awaiting confirmation. Revoke it and request a new one.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
-- 6. RLS and privileges, the house shape.
-- ---------------------------------------------------------------------------
ALTER TABLE app.render_share_confirmations ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.render_share_confirmations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS render_share_confirmations_strict ON app.render_share_confirmations;
CREATE POLICY render_share_confirmations_strict ON app.render_share_confirmations
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    );

-- The Console writes it; the public share reader has no business seeing who
-- authorized the exit. DELETE is granted because the erasure hatch above needs
-- the privilege as well as the trigger clause (migration 277's posture).
GRANT SELECT, INSERT, UPDATE, DELETE ON app.render_share_confirmations TO connector;
REVOKE ALL ON app.render_share_confirmations FROM toorow_share_reader;

COMMIT;
