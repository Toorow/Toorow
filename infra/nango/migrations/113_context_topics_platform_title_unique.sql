-- infra/nango/migrations/113_context_topics_platform_title_unique.sql
--
-- Story 44.2 (review wave 2, finding #4): put a real DB constraint behind the
-- day-0 seed's idempotency key.
--
-- core/context_seed.py keys a seeded connector topic on
-- (title, platform scope) -- "Connector: <display name>" with project_id NULL --
-- and enforces it with a SELECT-then-INSERT. That is a check-then-act: two
-- seeds racing (a project creation and a landing hook, two workers, two Cloud
-- Run instances) both see "no topic" and both insert. The result is duplicate
-- platform topics that no later run ever reconciles, because _find_platform_topic
-- takes ORDER BY created_at LIMIT 1 and silently ignores the twin.
--
-- This migration makes the key an invariant:
--   UNIQUE (title) WHERE project_id IS NULL AND status <> 'archived'
--
-- Partial on two counts:
--   * project_id IS NULL -- PROJECT-scoped topics are free to reuse a title in
--     each project; only the platform namespace is a single namespace.
--   * status <> 'archived' -- archiving is how a curator retires a topic; an
--     archived "Connector: Meta Ads" must not block seeding a fresh one. This
--     is also what makes the duplicate cleanup below possible at all.
--
-- The seed side of the same fix: context_seed wraps create_topic in a SAVEPOINT
-- and, on UniqueViolation, re-reads the winner and carries on instead of
-- aborting the transaction.
--
-- R44-NFR01 / schema-drift note: the Supabase schema is NOT applied in numeric
-- order (095/097 applied, 102/104/105 not, 106 out of sequence -- see the
-- schema drift memory). So this migration proves nothing from "031 < 113": it
-- asks to_regclass for the actual objects and skips loudly rather than failing
-- a whole migration run on an environment that never had the context layer.
--
-- Idempotent: CREATE UNIQUE INDEX IF NOT EXISTS, and the cleanup is a no-op
-- once no duplicates remain. Running it twice changes nothing.

BEGIN;

DO $$
DECLARE
    archived_count INTEGER := 0;
    remaining_dupes INTEGER := 0;
BEGIN
    IF to_regclass('app.context_topics') IS NULL THEN
        RAISE NOTICE
            'app.context_topics is absent -- migration 031 was never applied here; '
            'skipping 113 entirely (nothing to constrain)';
        RETURN;
    END IF;

    -- -----------------------------------------------------------------------
    -- 1. Deterministic duplicate cleanup.
    --
    -- CREATE UNIQUE INDEX fails outright if duplicates already exist, so they
    -- must go first. Rule: for each duplicated platform title, the OLDEST row
    -- wins (created_at, then id as a stable tie-break -- ids are ULIDs, so the
    -- tie-break is itself chronological); every other row is ARCHIVED, never
    -- deleted. Archiving preserves the rows and their version history for
    -- 44.8, and drops them out of the partial index predicate.
    -- -----------------------------------------------------------------------

    IF to_regclass('app.context_topics_versions') IS NOT NULL THEN
        -- Record the archival in the append-only history so 44.8 does not show
        -- a status flipping with no author.
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY title ORDER BY created_at, id
                   ) AS rn
            FROM app.context_topics
            WHERE project_id IS NULL
              AND status <> 'archived'
        ),
        losers AS (
            SELECT id FROM ranked WHERE rn > 1
        ),
        updated AS (
            UPDATE app.context_topics t
            SET status = 'archived',
                updated_at = now()
            WHERE t.id IN (SELECT id FROM losers)
            RETURNING t.id, t.project_id, t.title, t.body_md, t.status,
                      t.created_by, t.created_at, t.updated_at
        )
        INSERT INTO app.context_topics_versions
            (topic_id, project_id, title, body_md, status, created_by,
             created_at, updated_at, version_number, changed_by, changed_at)
        SELECT u.id, u.project_id, u.title, u.body_md, u.status, u.created_by,
               u.created_at, u.updated_at,
               COALESCE(
                   (SELECT max(v.version_number)
                    FROM app.context_topics_versions v
                    WHERE v.topic_id = u.id),
                   0
               ) + 1,
               'system:migration-113',
               now()
        FROM updated u;

        GET DIAGNOSTICS archived_count = ROW_COUNT;
    ELSE
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY title ORDER BY created_at, id
                   ) AS rn
            FROM app.context_topics
            WHERE project_id IS NULL
              AND status <> 'archived'
        ),
        losers AS (
            SELECT id FROM ranked WHERE rn > 1
        )
        UPDATE app.context_topics t
        SET status = 'archived',
            updated_at = now()
        WHERE t.id IN (SELECT id FROM losers);

        GET DIAGNOSTICS archived_count = ROW_COUNT;
    END IF;

    IF archived_count > 0 THEN
        RAISE NOTICE
            'archived % duplicate platform-scoped context topic(s) before '
            'creating uq_context_topics_title_platform (oldest row kept per title)',
            archived_count;
    END IF;

    -- Belt and braces: prove the cleanup actually cleared the way. If it did
    -- not, say why instead of letting CREATE UNIQUE INDEX emit a bare
    -- "could not create unique index" with a key value.
    SELECT count(*) INTO remaining_dupes
    FROM (
        SELECT title
        FROM app.context_topics
        WHERE project_id IS NULL AND status <> 'archived'
        GROUP BY title
        HAVING count(*) > 1
    ) d;

    IF remaining_dupes > 0 THEN
        RAISE EXCEPTION
            '% platform topic title(s) are still duplicated after cleanup -- '
            'refusing to create uq_context_topics_title_platform',
            remaining_dupes;
    END IF;

    -- -----------------------------------------------------------------------
    -- 2. The constraint itself.
    -- -----------------------------------------------------------------------

    EXECUTE $ddl$
        CREATE UNIQUE INDEX IF NOT EXISTS uq_context_topics_title_platform
            ON app.context_topics (title)
            WHERE project_id IS NULL AND status <> 'archived'
    $ddl$;

    EXECUTE $cmt$
        COMMENT ON INDEX app.uq_context_topics_title_platform IS
            'Story 44.2 -- the day-0 seed keys platform topics on their title '
            '("Connector: <display name>"). Without this index two concurrent '
            'seeds both pass the SELECT and both INSERT. Partial: project-scoped '
            'topics keep their own namespace, and archived topics do not block '
            'a re-seed.'
    $cmt$;
END
$$;

COMMIT;
