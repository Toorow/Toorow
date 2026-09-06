-- 344 -- the platform role may become the share reader.
--
-- WHAT WAS MEASURED, 2026-09-04 (G15-T07, then `render_shares_api` logging the
-- exception type on mcp-server-00237): every `POST /api/render-shares/exchange`
-- in production ended in `InsufficientPrivilege` and the recipient read « This
-- shared result is not available ». `render_share_connection()` opens the
-- platform connection -- the `connector` role -- and issues
-- `SET LOCAL ROLE toorow_share_reader`. Migration 162 created that reader role
-- and granted it `TO current_user`, which was the OWNER running the migration
-- (`postgres`), never `connector`. In production the only login members of the
-- reader were `postgres` and `supabase_admin`, so the platform could not switch
-- to it, and no share link -- Render or Dossier -- has ever opened since the role
-- exists. A replay from a workstation whose DSN is the owner passed, which is
-- why it was not seen: the instrument measured its own copy.
--
-- THE REPAIR. The membership the server needs, granted to the role the server
-- is. Guarded, because a disposable database may run the migrations before the
-- role exists; there `scripts/disposable_postgres.py` creates `connector` first
-- and this grant lands.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'connector')
       AND NOT pg_has_role('connector', 'toorow_share_reader', 'member') THEN
        EXECUTE 'GRANT toorow_share_reader TO connector';
    END IF;
END $$;

COMMENT ON ROLE toorow_share_reader IS
    'The bounded reader the public share handlers switch to (162). Migration 344: granted to the platform role `connector`, which is the role that issues SET LOCAL ROLE.';
