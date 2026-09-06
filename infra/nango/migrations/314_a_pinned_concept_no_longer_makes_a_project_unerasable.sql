-- ============================================================================
-- 314 — a Concept a Semantic View pins no longer makes its Project unerasable
-- ============================================================================
--
-- THE CLAUSE THIS CLOSES. `docs/product-architecture/governance.md` wrote it on
-- 2026-08-18 and left it open: "A Project where a Semantic View pins a Concept
-- therefore cannot be erased. It is a defect of the eraser." It is the RGPD
-- path, so it is the one family of defects here that cannot wait.
--
-- MEASURED 2026-08-25, on a disposable Postgres at migration 313, with one
-- organization holding one Project, one published Semantic Concept and one
-- published Semantic View version pinning it:
--
--   FAILING OP: delete app.projects | fk_projects_org
--     DELETE FROM app.projects WHERE (org_id) IN (SELECT id FROM app.organizations WHERE id = %s)
--     ERROR: update or delete on table "semantic_concepts" violates foreign key
--            constraint "semantic_view_version_concepts_concept_id_fkey"
--            on table "semantic_view_version_concepts"
--
-- WHY IT IS THE PLAN AND NOT THE TABLE. `app.semantic_view_version_concepts`
-- appears in ZERO statements of the plan `core/org_purge.plan_purge` builds —
-- measured on the same run. Its only visible parent edge is
-- `view_version_id ... ON DELETE CASCADE`, and `_FK_GRAPH_SQL` filters to
-- `confdeltype IN ('a','r')`, so the graph cannot see it. The erasure of those
-- rows is therefore left entirely to Postgres, which reaches them the long way:
-- projects -> semantic_views -> semantic_view_versions -> the pin rows. On the
-- SAME statement Postgres also cascades projects -> semantic_concepts, and
-- `ON DELETE RESTRICT` is checked IMMEDIATELY — before the sibling cascade has
-- removed the rows that would have satisfied it. Two cascade branches of one
-- DELETE, and the order between them is not ours to choose.
--
-- THE REMEDY IS 312'S, APPLIED TO THE SAME FAULT ONE TABLE FURTHER IN: give the
-- graph an edge it CAN see. `semantic_concepts.project_id` becomes
-- ON DELETE NO ACTION. `plan_purge` then visits `app.semantic_concepts` from
-- `app.projects` and, post-order, emits an explicit DELETE for each of its
-- blocking children FIRST — `semantic_view_version_concepts`,
-- `semantic_view_version_bindings`, `semantic_concept_dependencies` — then for
-- the Concepts themselves, and only then for the Project. The pin is erased in
-- a named statement instead of a race between two cascades, and the erasure
-- becomes auditable in the plan's `rows_by_table`.
--
-- WHAT THIS DOES NOT WEAKEN. Nothing about immutability moves: no trigger is
-- created, dropped or re-armed, and `app.rgpd_erasure` is untouched. NO ACTION
-- is STRICTER than the CASCADE it replaces, not looser — outside an erasure, a
-- Project holding Semantic Concepts is now refused instead of silently taking
-- them with it. The one caller that deletes a project row for real,
-- `core/projects_api.py`, does it to roll back a project created seconds
-- earlier, which owns no Concept; the test fixture at
-- `server/tests/conftest.py:447` already catches `ForeignKeyViolation` on that
-- fast path and falls back to `plan_purge` rooted on the project — the same
-- plan this migration completes.
--
-- WHY NOT FLIP THE RESTRICT TO NO ACTION INSTEAD. It would move the check to
-- the end of the statement and hope the cascade that empties the table is
-- queued before the check that reads it. That is the same race, decided by
-- trigger-queue order rather than by the plan. An erasure must not depend on it.
--
-- WHY `fk_semantic_concept_version_scope` STAYS CASCADE. Making it visible too
-- would close the cycle `semantic_concepts <-> semantic_concept_versions`
-- (`current_version_id`, `pending_version_id`, `last_known_good_version_id` all
-- NO ACTION), and `plan_purge` breaks a cycle by NULLing every NULLABLE column
-- of the back-reference — which on `fk_semantic_concepts_current_version`
-- (current_version_id, id, project_id) includes `project_id`. That would set a
-- Project's Concepts to platform scope mid-purge and leave them behind forever.
-- The versions keep leaving by CASCADE from the Concept, after its pins are
-- gone.
--
-- Schema-Change-Checklist: idempotent (the DO block re-runs to the same state),
-- no data touched, no privilege changed, no trigger touched. No migration below
-- this number is edited. This is migration 314.
-- ============================================================================

BEGIN;

DO $$
DECLARE
    _constraint text;
BEGIN
    SELECT conname INTO _constraint
    FROM pg_constraint
    WHERE conrelid = 'app.semantic_concepts'::regclass
      AND contype = 'f'
      AND confrelid = 'app.projects'::regclass
      AND confdeltype = 'c';

    IF _constraint IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE app.semantic_concepts DROP CONSTRAINT %I',
            _constraint
        );
        ALTER TABLE app.semantic_concepts
            ADD CONSTRAINT semantic_concepts_project_id_fkey
            FOREIGN KEY (project_id) REFERENCES app.projects(id)
            ON DELETE NO ACTION;
    END IF;
END $$;

COMMENT ON CONSTRAINT semantic_concepts_project_id_fkey ON app.semantic_concepts IS
    'NO ACTION, not CASCADE, so core/org_purge.plan_purge can SEE this edge and '
    'erase the Semantic View pins (semantic_view_version_concepts) before the '
    'Concept they point at. Migration 314 — governance.md, RGPD erasure.';

COMMIT;
