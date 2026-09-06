-- 230 -- the one inherited row that made two guards unvalidatable, forever
--
-- WHY THIS EXISTS. `183_inbound_credential_lifecycle_guard.sql:216-237` left
-- `fk_dic_datastream` and `ck_dic_operation_required` NOT VALID, named the
-- reason at its line, and said the VALIDATE "reviendra dans une migration
-- ulterieure, quand la ligne aura ete traitee par qui la possede". Nobody owns
-- it: it is a Mailgun probe artifact from 2026-07-25 whose Datastream was
-- deleted afterwards. AI-139 tracked the wait.
--
-- THE WAIT COULD NOT END ON ITS OWN, and that is the part 183 could not see.
-- `app.protect_inbound_credential` refuses every DELETE unconditionally --
-- "datastream_inbound_credentials rows may not be deleted", ERRCODE 23000 --
-- and migration 200 armed its trigger with the single hatch this schema has:
--
--     WHEN (COALESCE(current_setting('app.rgpd_erasure', true), 'off') <> 'on')
--
-- So the row could only ever leave through a tenant erasure, and its tenant is
-- not being erased. Left alone, both constraints stay NOT VALID for the life of
-- the product while every new row is checked -- a guard that is honest going
-- forward and permanently unprovable backwards.
--
-- MEASURED 2026-08-08, in production, before writing this file:
--
--   rows_total 2 | violating_check 1 | violating_fk 1     -- the same single row
--   receipts referencing that row .......................  0
--   app.inbound_receipts is the ONLY table with a FK to it
--   the surviving row (2026-08-07) violates neither predicate
--
-- WHAT THIS MIGRATION CLAIMS, AND WHAT IT DOES NOT. It does not widen the
-- append-only rule: `protect_inbound_credential` is untouched, and after this
-- migration a DELETE is refused exactly as it is today. It borrows the erasure
-- hatch for one statement, on a predicate that cannot match a live credential:
-- no operation, no Datastream, no receipt. A credential in any of those three
-- relations is out of reach of this WHERE clause by construction.
--
-- SET LOCAL dies on COMMIT and on ROLLBACK, so the hatch cannot outlive this
-- transaction even if the migration fails halfway.

BEGIN;

SET LOCAL app.rgpd_erasure = 'on';

DELETE FROM app.datastream_inbound_credentials c
 WHERE c.operation_id IS NULL
   AND NOT EXISTS (SELECT 1 FROM app.datastreams d WHERE d.id = c.datastream_id)
   AND NOT EXISTS (SELECT 1 FROM app.inbound_receipts r WHERE r.credential_id = c.id);

SET LOCAL app.rgpd_erasure = 'off';

-- Both VALIDATEs are the point of the file. If a row this predicate did not
-- reach still violates either one, the migration fails HERE rather than
-- reporting success on a guard that still proves nothing.
ALTER TABLE app.datastream_inbound_credentials
    VALIDATE CONSTRAINT fk_dic_datastream;
ALTER TABLE app.datastream_inbound_credentials
    VALIDATE CONSTRAINT ck_dic_operation_required;

COMMIT;
