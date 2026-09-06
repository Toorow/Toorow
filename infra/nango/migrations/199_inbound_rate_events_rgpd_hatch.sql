-- 199 -- the anti-abuse ledger blocked RGPD erasure, and it was right to
--
-- Migration 183:528 revokes EVERYTHING on
-- `app.datastream_inbound_credential_rate_events` from PUBLIC and from
-- `connector`, and writes it only through a SECURITY DEFINER function. That is
-- not an oversight to undo: the table counts credential issue/rotate/resolve
-- operations in order to REFUSE the next one. An application role that could
-- delete its own rows could erase its own quota history and issue without limit.
-- The lockdown is the control.
--
-- But `DELETE /api/organizations` must reach it. The rows hang off
-- `app.datastreams` (FK `..._datastream_id_fkey`), so they sit inside the tenant
-- tree `core/org_purge.py` walks, and the erasure stopped there with 42501:
--
--   psycopg.errors.InsufficientPrivilege: permission denied for table
--   datastream_inbound_credential_rate_events
--
-- Measured 2026-08-03 on a freshly migrated database (198 migrations, ordinary
-- `connector` role, no superuser, no BYPASSRLS): with 198 in place the purge
-- cleared `org_plan_history` and stopped on THIS table instead. It is the next
-- link of the same chain, not a repetition of it.
--
-- THE SHAPE IS 098'S, DELIBERATELY. Migration 098 met the same conflict on
-- append-only ledgers -- a table that must refuse DELETE, inside a tree that
-- must be erasable -- and resolved it with a flag the purge sets and a trigger
-- that judges it. Reusing that idiom keeps ONE answer to "how does erasure get
-- through a protected table"; inventing a second would mean the next protected
-- table has two precedents to choose between.
--
-- WHAT THIS GRANT DOES NOT OPEN. DELETE, plus SELECT on TWO COLUMNS. INSERT and
-- UPDATE stay revoked, so `connector` still cannot forge a quota row or rewrite
-- one, and the trigger below refuses the DELETE too unless the caller has
-- flagged itself as an erasure. `SET LOCAL app.rgpd_erasure` dies with the
-- transaction, on commit AND on rollback, so it cannot leak into a pooled
-- session. The counting path is untouched: it goes through the SECURITY DEFINER
-- function, which owns its own privileges.
--
-- WHY SELECT AT ALL, AND WHY EXACTLY TWO COLUMNS. Granting DELETE alone leaves
-- the statement refused, which cost an experiment to learn rather than assume:
-- with `has_table_privilege(... 'DELETE') = true`, the DELETE still failed with
-- "permission denied". PostgreSQL checks SELECT on every column a WHERE clause
-- READS, and `org_purge._child_predicate` (org_purge.py:168-171) builds
-- `(<fk cols>) IN (SELECT ... )`. So the purge needs to read the foreign-key
-- columns to find its rows -- and nothing else.
--
-- Column-level grants keep that minimal. `datastream_id` and `operation_id` are
-- the only two foreign keys this table carries, so they are the only two the
-- traversal can enter by; `channel`, `operation` and `created_at` -- the columns
-- that ARE the quota history -- stay unreadable to `connector`. Reading which
-- datastream a row belongs to tells nobody how many operations it has left.

BEGIN;

CREATE OR REPLACE FUNCTION app.guard_inbound_rate_events_delete()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    -- current_setting(..., TRUE) returns NULL rather than raising when the flag
    -- was never set, which is the ordinary case: an unflagged DELETE is refused.
    IF coalesce(current_setting('app.rgpd_erasure', TRUE), 'off') <> 'on' THEN
        RAISE EXCEPTION
            'datastream_inbound_credential_rate_events is an anti-abuse ledger:'
            ' rows may only be deleted by a flagged RGPD erasure'
            USING ERRCODE = '42501';
    END IF;
    RETURN OLD;
END;
$$;

DROP TRIGGER IF EXISTS trg_guard_inbound_rate_events_delete
    ON app.datastream_inbound_credential_rate_events;

CREATE TRIGGER trg_guard_inbound_rate_events_delete
    BEFORE DELETE ON app.datastream_inbound_credential_rate_events
    FOR EACH ROW EXECUTE FUNCTION app.guard_inbound_rate_events_delete();

GRANT DELETE ON app.datastream_inbound_credential_rate_events TO connector;
GRANT SELECT (datastream_id, operation_id)
    ON app.datastream_inbound_credential_rate_events TO connector;

-- The privilege is only half of the gate. This table carries RLS in FORCE mode
-- with policies for SELECT (`r`) and INSERT (`a`) and NONE for DELETE -- and a
-- command with no policy matches no row, silently. So the grants above, alone,
-- turned a loud 42501 into a purge that SUCCEEDS having deleted nothing: the
-- quota history would outlive the erasure and the endpoint would report it done.
-- That is strictly worse than the refusal it replaced, and it is only visible if
-- you count rows rather than check the exit code.
--
-- The policy repeats the trigger's condition rather than deferring to it,
-- because they act at different moments and neither subsumes the other: the
-- policy decides which rows the statement may SEE, the trigger refuses a row it
-- reaches. Keeping both means a future policy that widens by accident still
-- meets a refusal instead of erasing an anti-abuse ledger.
DROP POLICY IF EXISTS inbound_credential_rate_events_erase
    ON app.datastream_inbound_credential_rate_events;

CREATE POLICY inbound_credential_rate_events_erase
    ON app.datastream_inbound_credential_rate_events
    FOR DELETE
    USING (coalesce(current_setting('app.rgpd_erasure', TRUE), 'off') = 'on');

COMMIT;
