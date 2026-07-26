-- 108_knowledge_entries_to_context_topics.sql
--
-- Story 44.1 -- one-shot, idempotent copy of the demo app.knowledge_entries rows
-- into the governed context store (app.context_topics + version 1 rows in
-- app.context_topics_versions), so KnowledgeBasePage can read exclusively from
-- the governed store without losing whatever was authored under the old demo
-- surface. Does NOT drop app.knowledge_entries -- GET /api/knowledge stays live
-- (server/core/admin_api.py), now flagged with a `Deprecation: true` header, for
-- anything that still reads it.
--
-- Idempotency: the copied topic id is DETERMINISTIC, derived from the source
-- row id (top_legacy_<md5(knowledge_entries.id)>), so a second run of this file
-- hits the app.context_topics primary key (ON CONFLICT DO NOTHING) and inserts
-- nothing new. A second guard -- NOT EXISTS on the same title within the same
-- scope -- also skips a source row whose title was already authored straight
-- into context_topics (by a human, or by a previous run under a different id
-- scheme): never overwrite or duplicate a governed topic that already exists.
--
-- Schema is not linear (see [[supabase-migrations-applied-094]]) -- guard on
-- actual object state via to_regclass, never assume 031/095 ran first on every
-- database this file is played against (fixtures rebuild chains from scratch;
-- some environments may not have the demo 095 tables at all).
--
-- Demo rows are NOT governed content: migration 095 seeded three fabricated
-- rows into the 'default' project ('ROAS calculation & deduplication policy',
-- 'Canonical revenue vs commerce gross sales', 'Post-click attribution
-- reconciliation procedure', authored by the fictional 'Winston (Architect)' /
-- 'Mary (Analyst)' / 'Paige (Tech Writer)' identities used across seed data).
-- Promoting those literals into the governed corpus would present fabricated
-- demo content as real curated knowledge. This migration therefore (a) excludes
-- those exact rows from the copy into app.context_topics, and (b) deletes them
-- outright from app.knowledge_entries so they cannot be surfaced by the
-- (deprecated) GET /api/knowledge fallback either. The delete is guarded on the
-- exact (project_id, title, author) tuple, so it is a no-op -- not an error --
-- on a database where those demo rows were never seeded or already removed.

BEGIN;

DO $migrate_knowledge_to_topics$
DECLARE
    migrated_count integer := 0;
    audit_row_id text;
BEGIN
    IF to_regclass('app.knowledge_entries') IS NULL THEN
        RAISE NOTICE '108: app.knowledge_entries absent -- nothing to migrate';
        RETURN;
    END IF;

    -- Demo seed rows fabricated by migration 095 -- never governed content,
    -- never copied, and removed from the demo table outright (see header).
    -- Runs BEFORE the context-store guard: a database that has the 095 demo
    -- table but not the 031 governed store must still lose the fabricated
    -- rows (they would otherwise stay live behind the deprecated
    -- GET /api/knowledge).
    DELETE FROM app.knowledge_entries
    WHERE project_id = 'default'
      AND (title, author) IN (
        ('ROAS calculation & deduplication policy', 'Winston (Architect)'),
        ('Canonical revenue vs commerce gross sales', 'Mary (Analyst)'),
        ('Post-click attribution reconciliation procedure', 'Paige (Tech Writer)')
      );

    IF to_regclass('app.context_topics') IS NULL
       OR to_regclass('app.context_topics_versions') IS NULL THEN
        RAISE NOTICE '108: governed context store absent (migration 031) -- skipping copy';
        RETURN;
    END IF;

    -- Copy each remaining knowledge_entries row into context_topics, skipping
    -- any title that already exists in the same scope (platform NULL vs the
    -- row's project_id). knowledge_entries.project_id is NOT NULL (095), so
    -- every migrated topic lands project-scoped, matching its source exactly.
    -- The primary statement of this WITH is the context_topics_versions
    -- INSERT (referencing `inserted`), so GET DIAGNOSTICS below reports the
    -- number of version-1 rows actually created this run -- i.e. the count of
    -- topics genuinely migrated (skipped/conflicting rows are excluded).
    WITH inserted AS (
        INSERT INTO app.context_topics
            (id, project_id, title, body_md, status, created_by, created_at, updated_at)
        SELECT
            'top_legacy_' || md5('knowledge_entries:' || ke.id),
            ke.project_id,
            ke.title,
            CASE
                WHEN ke.topic IS NOT NULL AND btrim(ke.topic) <> ''
                    THEN '**Topic:** ' || btrim(ke.topic) || E'\n\n' || COALESCE(ke.body, '')
                ELSE COALESCE(ke.body, '')
            END,
            'active',
            COALESCE(NULLIF(btrim(ke.author), ''), 'system:knowledge-migration'),
            COALESCE(ke.created_at, now()),
            COALESCE(ke.updated_at, ke.created_at, now())
        FROM app.knowledge_entries ke
        WHERE NOT EXISTS (
            SELECT 1 FROM app.context_topics ct
            WHERE ct.title = ke.title
              AND ct.project_id IS NOT DISTINCT FROM ke.project_id
        )
        ON CONFLICT (id) DO NOTHING
        RETURNING id, project_id, title, body_md, status, created_by, created_at, updated_at
    )
    INSERT INTO app.context_topics_versions
        (topic_id, project_id, title, body_md, status, created_by, created_at, updated_at,
         version_number, changed_by, changed_at)
    SELECT
        id, project_id, title, body_md, status, created_by, created_at, updated_at,
        1, created_by, created_at
    FROM inserted
    ON CONFLICT (topic_id, version_number) DO NOTHING;

    GET DIAGNOSTICS migrated_count = ROW_COUNT;

    -- Audit trail: one summary row for this migration run, only when it
    -- actually migrated something (idempotent -- a re-run that migrates
    -- nothing writes no audit row). audit_log.connection_ref is nullable
    -- (021_project_members.sql) and its FK was dropped (098), so a
    -- migration-authored row with no connection is valid.
    IF migrated_count > 0 AND to_regclass('app.audit_log') IS NOT NULL THEN
        -- NOTE: id is a uuid-derived TEXT, not a real ULID like application-
        -- authored audit rows (002 documents 'audit_' + ULID). Deviation is
        -- deliberate and accepted for this one migration-authored summary row:
        -- uniqueness and the 'audit_' prefix convention hold; only the
        -- timestamp-sortable property of ULIDs is lost (created_at carries it).
        audit_row_id := 'audit_' || replace(gen_random_uuid()::text, '-', '');
        INSERT INTO app.audit_log
            (id, identity, action, provider_account, connection_ref, metadata, created_at)
        VALUES
            (
                audit_row_id,
                'system:knowledge-migration',
                'context_topic.migrated',
                'platform',
                NULL,
                jsonb_build_object(
                    'source', 'knowledge_entries',
                    'migrated_count', migrated_count
                ),
                now()
            );
    END IF;

    RAISE NOTICE '108: knowledge_entries -> context_topics migration complete (% rows)', migrated_count;
END;
$migrate_knowledge_to_topics$;

COMMIT;
