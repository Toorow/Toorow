-- A Datastream names its own Source Account.
--
-- WHAT WAS WRONG. `glossary.md` ratifies the split: one Source Authorization
-- opens several Connectors (one Google consent covers GA4, GSC, Google Ads,
-- Sheets), exposes many Source Accounts, and *"a Datastream selects one
-- exposed scope"*. The schema said something else. The selected account lived
-- in `app.connection_account_scope` under a UNIQUE index on
-- `(connection_ref_id)` -- migration 046, commented "ONE living scope per
-- credential". So an authorization could name exactly ONE account, for every
-- Connector and every Datastream reached through it.
--
-- The consequences were all silent, none of them errors:
--   * ten GA4 properties under one consent -> nine unreachable;
--   * GSC and Google Ads under one consent -> the second selection OVERWROTE
--     the first, because the upsert conflicted on the connection alone;
--   * two Datastreams on two properties -> both pulled the same one.
--
-- `app.datastreams` carried no account column at all, so nothing could have
-- said otherwise: measured on 2026-08-05, the 43 connector Datastreams of this
-- deployment all pointed at one credential with `config` NULL, and the chosen
-- property appeared only inside a display name.
--
-- WHAT THIS DOES. Moves the binding to the object the glossary puts it on. The
-- Datastream names its Source Account; the authorization stops holding a
-- selection on everyone's behalf; `connection_account_scope` keeps its real job
-- -- *this account was selected and its access VERIFIED* -- once per account
-- instead of once per credential.
--
-- Additive and replayable. The column is NULLable: `external_bq` and
-- `managed_feed` Datastreams have no provider account, and NULL there means
-- "not applicable", not "unknown".
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. The pair a Datastream binds to has to be addressable AS a pair.
--
--    `source_account_id` is already globally unique, but the composite FK below
--    is what forbids a Datastream from naming an account belonging to a
--    DIFFERENT authorization than the one it points at -- a mismatch no CHECK
--    can express and application code kept having to re-verify.
-- ---------------------------------------------------------------------------
ALTER TABLE app.credential_accounts
    DROP CONSTRAINT IF EXISTS uq_credential_accounts_credential_source_account;
ALTER TABLE app.credential_accounts
    ADD CONSTRAINT uq_credential_accounts_credential_source_account
    UNIQUE (credential_id, source_account_id);

-- ---------------------------------------------------------------------------
-- 2. The binding.
-- ---------------------------------------------------------------------------
ALTER TABLE app.datastreams
    ADD COLUMN IF NOT EXISTS source_account_id TEXT;

COMMENT ON COLUMN app.datastreams.source_account_id IS
    'The Source Account this Datastream reads, as app.credential_accounts.source_account_id. NULL for external_bq and managed_feed Datastreams, which have no provider account. The extraction reads THIS, not the authorization-wide selection.';

-- ---------------------------------------------------------------------------
-- 3. Backfill A -- the setup path already recorded the operator's choice, it
--    just never reached a column: `datastream_activation.py` writes it to
--    `config.source_owner.selected_account_ref`.
-- ---------------------------------------------------------------------------
UPDATE app.datastreams d
SET source_account_id = ca.source_account_id
FROM app.credential_accounts ca
WHERE d.source_account_id IS NULL
  AND d.connection_ref_id IS NOT NULL
  AND ca.credential_id = d.connection_ref_id
  AND ca.source_account_id = d.config -> 'source_owner' ->> 'selected_account_ref';

-- ---------------------------------------------------------------------------
-- 4. Backfill B -- rows created before that path existed. The account they get
--    is the one their pulls ACTUALLY used: the single ready scope of their
--    authorization, which is what `queue._resolve_selected_account` handed to
--    every extraction. This records a fact rather than inventing a choice.
--
--    Deliberately not backfilled: a Datastream whose authorization has no ready
--    scope. Its account is unknown, and NULL says so.
-- ---------------------------------------------------------------------------
UPDATE app.datastreams d
SET source_account_id = ca.source_account_id
FROM app.connection_account_scope s
     JOIN app.credential_accounts ca
       ON ca.credential_id = s.connection_ref_id
      AND ca.external_account_id = s.account_id
WHERE d.source_account_id IS NULL
  AND d.connection_ref_id = s.connection_ref_id
  AND s.state = 'ready';

-- ---------------------------------------------------------------------------
-- 5. The pair constraint. MATCH SIMPLE: a row with either column NULL is not
--    checked, which is exactly the transition state -- a connector Datastream
--    whose account is still unknown stays legal, and a Datastream with no
--    authorization at all is unaffected.
-- ---------------------------------------------------------------------------
ALTER TABLE app.datastreams
    DROP CONSTRAINT IF EXISTS fk_datastreams_source_account;
ALTER TABLE app.datastreams
    ADD CONSTRAINT fk_datastreams_source_account
    FOREIGN KEY (connection_ref_id, source_account_id)
    REFERENCES app.credential_accounts (credential_id, source_account_id)
    ON DELETE RESTRICT;

CREATE INDEX IF NOT EXISTS idx_datastreams_source_account
    ON app.datastreams (source_account_id)
    WHERE source_account_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 6. An authorization may now have SEVERAL verified accounts.
--
--    The old unique index is the line that forbade ten GA4 properties. It is
--    replaced by one row per (authorization, account), plus one pending row per
--    authorization for the "selection not made yet" state, whose `account_id`
--    is NULL and which a partial index keys on its own.
-- ---------------------------------------------------------------------------
DROP INDEX IF EXISTS app.uq_connection_account_scope_connection;

CREATE UNIQUE INDEX IF NOT EXISTS uq_connection_account_scope_account
    ON app.connection_account_scope (connection_ref_id, account_id)
    WHERE account_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_connection_account_scope_pending
    ON app.connection_account_scope (connection_ref_id)
    WHERE account_id IS NULL;

COMMENT ON TABLE app.connection_account_scope IS
    'One row per (Source Authorization, Source Account) recording that the account was selected and its access VERIFIED. It is NOT the binding: which account a Datastream reads is app.datastreams.source_account_id (migration 211). Until 211 a UNIQUE index kept one row per credential, which capped every authorization at a single usable account.';

COMMIT;
