-- 318 -- the head of a context relation moves on FOUR columns, and the
--        privilege now says so.
--
-- THE FINDING (adversarial review of story 49-6, 2026-08-28, non-blocking):
-- migration 317 granted `UPDATE` on the whole of `app.context_relationships`.
-- As role `connector`, `UPDATE app.context_relationships SET target_id = 'x'
-- WHERE id = <crel>` was ALLOWED (rowcount=1) -- rewriting an endpoint of the
-- head with no version row, desyncing it from the chain the versions table
-- refuses to rewrite. Unreachable from code (`context_relationships.py` moves
-- only `status`, `projection_edge_id`, `current_version_id`, `updated_at`), but
-- a privilege that is wider than every writer is the posture migration 316
-- closed on eight other tables: a grant is the statement of what MAY happen,
-- and here it stated more than the design allows.
--
-- WHAT CHANGES. Table-level UPDATE is revoked and re-granted on the four
-- columns the service moves. Identity (`id`, `org_id`, `project_id`), the two
-- endpoints, the kind, the provenance and the creator become immutable for
-- `connector` -- a change of endpoint is a new relation, exactly as the story
-- says a change of version is a new version.
--
-- WHAT DOES NOT CHANGE. SELECT, INSERT, DELETE on the head (DELETE is the RGPD
-- erasure path, and cascades into the versions under the hatch). Nothing on
-- `app.context_relationship_versions`. No row is touched.
--
-- REPLAYABLE. Wrapped in the `pg_roles` check migrations 049, 056, 277, 316 and
-- 317 use; a no-op where `connector` does not exist. A column-level REVOKE of a
-- privilege not held is itself a no-op.

BEGIN;

DO $migration$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'connector') THEN
        RAISE NOTICE
            '318: role `connector` does not exist on this cluster -- nothing to '
            'narrow.';
        RETURN;
    END IF;

    EXECUTE 'REVOKE UPDATE ON app.context_relationships FROM connector';
    EXECUTE 'GRANT UPDATE (status, projection_edge_id, current_version_id, updated_at) '
            'ON app.context_relationships TO connector';

    RAISE NOTICE
        '318: UPDATE on app.context_relationships narrowed to status, '
        'projection_edge_id, current_version_id, updated_at for `connector`.';
END
$migration$;

COMMIT;
