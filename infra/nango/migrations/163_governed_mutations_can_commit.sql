-- Repair: EVERY governed mutation currently fails at COMMIT under the ordinary
-- application role. This is not a Story 50.7 defect; Story 50.7 is simply the
-- story that ran `execute_operation` against a non-superuser database and found it.
--
-- HOW TO REPRODUCE IT, exactly, before and after this migration:
--
--     INSERT INTO app.audit_log (id, identity, action, provider_account, connection_ref)
--          VALUES ('audit_probe', 't', 'test.probe', 'p', '');
--     INSERT INTO app.operations
--            (id, command_type, actor, resource_path, host_context, versions,
--             provider_references, request_hash, idempotency_key_hash,
--             confirmation_mode, state, audit_event_id)
--          VALUES ('op_probe', 'probe', 't', '[]'::jsonb, '{}'::jsonb, '{}'::jsonb,
--                  '{}'::jsonb, repeat('a',64), repeat('b',64), 'none', 'pending',
--                  'audit_probe');
--     COMMIT;   -- before: ERROR 42501, permission denied for table audit_log
--
-- THE TWO MIGRATIONS THAT COLLIDE, neither of them wrong on its own:
--
--   * `002_create_audit_log.sql:53` -- `REVOKE UPDATE, DELETE ON app.audit_log
--     FROM connector`, with the comment "This is the canonical enforcement layer".
--   * `060_operation_audit_outbox.sql:121-123` -- `fk_operations_audit_event`,
--     `FOREIGN KEY (audit_event_id) REFERENCES app.audit_log(id) DEFERRABLE
--     INITIALLY DEFERRED`.
--
-- A referential-integrity check on the REFERENCED side takes a row lock:
--     SELECT 1 FROM ONLY "app"."audit_log" x WHERE "id" = $1 FOR KEY SHARE OF x
-- and PostgreSQL requires UPDATE (or DELETE, or SELECT FOR UPDATE/SHARE) privilege
-- for any `FOR ... SHARE`/`FOR ... UPDATE` clause -- plain SELECT is not enough
-- (https://www.postgresql.org/docs/17/sql-select.html, "The Locking Clause").
-- Migration 002 removed exactly that privilege, so migration 060's FK can never be
-- satisfied by the role the application actually runs as. Because the constraint is
-- DEFERRED, the failure surfaces at COMMIT rather than at the INSERT, which is why
-- it reads as an infrastructure fault rather than a permission model bug.
--
-- WHY GRANTING `UPDATE` BACK DOES NOT WEAKEN ANYTHING. The grant was never the only
-- enforcement, and it was not even the strongest. `app.audit_log` carries
-- `audit_log_append_only`, a BEFORE UPDATE OR DELETE FOR EACH ROW trigger that
-- raises 'app.audit_log is append-only (FR12): UPDATE blocked', plus
-- `audit_log_block_truncate`. A trigger holds against the TABLE OWNER; a revoked
-- privilege does not, and a revoked privilege is what broke the foreign key.
-- Measured on 2026-07-31, after this grant, as the ordinary `connector` role:
--
--     UPDATE app.audit_log ... -> refused: "app.audit_log is append-only (FR12)"
--     DELETE FROM app.audit_log ... -> refused: permission denied (DELETE not granted)
--
-- So the append-only invariant is unchanged, and the audit spine is reachable again.
--
-- ONLY `UPDATE` IS GRANTED. `DELETE` is left revoked: the lock clause needs one of
-- the two, and granting the narrower one keeps a second, independent layer under
-- the trigger for the destructive verb.
--
-- SCOPE NOTE. This is deliberately its own migration rather than a paragraph inside
-- 162: it repairs a platform-wide governance seam that Story 50.7 depends on but
-- does not own, and it must be readable and revertable on its own terms by whoever
-- owns `app.audit_log`.

BEGIN;

GRANT UPDATE ON app.audit_log TO connector;

COMMENT ON TABLE app.audit_log IS
    'Append-only audit spine. Enforcement is the audit_log_append_only trigger '
    '(BEFORE UPDATE OR DELETE, FOR EACH ROW), which holds even against the table '
    'owner. UPDATE is GRANTED -- not because rows may be updated (the trigger '
    'refuses that) but because migration 060''s deferred foreign key from '
    'app.operations.audit_event_id must take a FOR KEY SHARE row lock, and '
    'PostgreSQL requires UPDATE or DELETE privilege for any locking clause. '
    'Revoking UPDATE here makes every governed mutation fail at COMMIT with '
    'SQLSTATE 42501; see migration 163 for the reproduction.';

COMMIT;
