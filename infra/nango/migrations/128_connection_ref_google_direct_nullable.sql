-- infra/nango/migrations/128_connection_ref_google_direct_nullable.sql
--
-- Lets a Google-direct connection exist at all.
--
-- Migration 029 introduced the direct-OAuth path for the Google stack: it added
-- `auth_path` with CHECK (auth_path IN ('nango', 'google_direct')) and the token
-- columns that path needs. It did not touch `nango_connection_id NOT NULL`, and
-- no later migration did either -- so a google_direct row, which by definition
-- has no Nango connection, could never be inserted:
--
--   null value in column "nango_connection_id" of relation "connection_ref"
--   violates not-null constraint
--
-- Observed in production on 2026-07-27, the first time anyone clicked "Connect
-- Google". It had never fired before because nothing in the console could reach
-- that panel (the panel mounted only from an EXISTING google_direct row, and
-- "Add connection" excludes Google by construction). Repairing that reachability
-- earlier the same day is what exposed this: two defects in series, the second
-- one shielded by the first.
--
-- THE INVARIANT IS KEPT, NOT DROPPED
-- A Nango connection without its nango_connection_id is still meaningless, so the
-- guarantee moves from the column to a CHECK that states the actual rule:
-- required for the nango path, absent for the direct one. Simply making the
-- column nullable would have traded a wrong constraint for no constraint.
--
-- Existing rows are all auth_path='nango' with an id (the column was NOT NULL
-- until now), so none can violate the new CHECK -- verified before it is added.
--
-- Idempotent: DROP NOT NULL is a no-op once applied; the constraint is dropped
-- IF EXISTS before being re-added.

BEGIN;

DO $$
DECLARE
    offending INTEGER;
BEGIN
    IF to_regclass('app.connection_ref') IS NULL THEN
        RAISE EXCEPTION 'app.connection_ref is missing -- apply migration 001 first';
    END IF;

    SELECT count(*) INTO offending
      FROM app.connection_ref
     WHERE auth_path = 'nango' AND nango_connection_id IS NULL;
    IF offending > 0 THEN
        RAISE EXCEPTION
            '128: % nango connection(s) already have no nango_connection_id; '
            'resolve them before the CHECK is added', offending;
    END IF;

    ALTER TABLE app.connection_ref ALTER COLUMN nango_connection_id DROP NOT NULL;

    ALTER TABLE app.connection_ref
        DROP CONSTRAINT IF EXISTS ck_connection_ref_nango_id_required;
    ALTER TABLE app.connection_ref
        ADD CONSTRAINT ck_connection_ref_nango_id_required
        CHECK (auth_path <> 'nango' OR nango_connection_id IS NOT NULL);
END
$$;

COMMENT ON COLUMN app.connection_ref.nango_connection_id IS
    'Nango''s connection id. Required when auth_path = ''nango'' and absent for '
    'google_direct, which authorises against Google itself (AD-21). Enforced by '
    'ck_connection_ref_nango_id_required rather than by the column, because the '
    'rule depends on the path.';

COMMIT;
