-- A Source Account remembers WHICH Connector discovered it.
--
-- One Google consent opens Search Console, Analytics, Ads, Sheets and six more
-- (`connection_tools.GOOGLE_SCOPE_CONNECTORS`), and each of those tools returns
-- a DIFFERENT account list: `sc-domain:example.org` from Search Console,
-- `properties/123` from Analytics, a customer id from Ads. They all land in
-- `app.credential_accounts`, keyed `(credential_id, external_account_id)`, with
-- nothing recording which tool produced which row.
--
-- So the Datastream setup step could not answer its own first question. Asked
-- "which Connector goes with this account", the only available answer was the
-- authorization's whole Connector set -- ten of them for a Google grant, nine
-- of which cannot read the chosen scope at all.
--
-- Nullable on purpose: every row discovered before this column existed keeps a
-- NULL, and NULL means "unknown", not "none". The read side falls back to the
-- authorization-level set for those, which is exactly the behaviour they had.
-- A re-discovery fills the column in.

BEGIN;

ALTER TABLE app.credential_accounts
    ADD COLUMN IF NOT EXISTS discovered_for_connector TEXT;

COMMENT ON COLUMN app.credential_accounts.discovered_for_connector IS
    'Connector whose discovery call returned this scope; NULL when unknown (pre-210 rows).';

COMMIT;
