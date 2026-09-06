-- 336: AI-346 -- a Semantic View member's role is the Concept's kind, and a
-- compiled artifact is re-derived when the compiler moves.
--
-- Ratified in `docs/product-architecture/governance.md`, amendment of
-- 2026-09-01 ("a compiled artifact is derived, and it is re-derived when the
-- compiler moves").
--
-- MEASURED ON THE DEPLOYED PRODUCT, 2026-09-01. The published Semantic View
-- version of the reference Project carries 13 member rows in
-- `app.semantic_view_version_concepts`, and every one of them says
-- `role = 'metric'` -- the dimensions included. The writer stored
-- `entry.get("role") or "metric"` on the caller's word, while the compiler
-- derives the role from `app.semantic_concepts.kind`. Two authorities for one
-- fact; the stored one was wrong for every dimension of every View that was
-- published without a role in its payload.
--
-- PART 1 -- THE STORED ROWS. `role` becomes what `kind` says, on every row of
-- every Project where the two differ. Idempotent: a second run matches nothing.
-- The table has no immutability trigger (142 puts one on the version row and
-- on the artifact row, not on the member rows), and the CHECK on `role` admits
-- exactly the two values `kind` admits, so the UPDATE cannot violate it. The
-- writer (`semantic_model._apply_view`) derives the role from the head from
-- this migration on, so the mismatch cannot be rewritten.
--
-- PART 2 -- THE ATTEMPTS. When the compiler version moves, the product
-- re-derives every artifact a published version still executes through; the
-- sweep writes a NEW `app.semantic_compiled_artifacts` row per version
-- (`uq_semantic_compiled_artifact_version` makes that the only possible shape)
-- and never touches the version. What the sweep cannot recompile must be
-- NAMED where the person reads the refusal, and neither immutable table can
-- carry that: a version row is frozen, an artifact row is frozen. This table
-- is the record of the attempt -- one row per (version, compiler), upserted,
-- because an attempt is a fact about the newest try and not a version of
-- anything. `query_specs._load_pinned_view` reads it to say "the product tried
-- on <date> and could not (<codes>)" instead of "publish it again".
--
-- No `org_id` column, deliberately: the semantic tables of 142 are scoped by
-- `project_id` and carry no RLS policy of their own; this one follows them.
-- Both foreign keys cascade, so the org eraser (`core.org_purge.plan_purge`,
-- which walks the foreign keys) removes these rows with the version.
--
-- MEASURED BEFORE WRITING (disposable cluster on port 55432, ledger head 335):
--
--     SELECT count(*) FROM app.semantic_view_version_concepts m
--       JOIN app.semantic_concepts c ON c.id = m.concept_id
--      WHERE m.role <> c.kind;                                  -- local rows only
--
-- Not applied to production by the session that wrote it.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. The stored member roles become the Concept's kind.
-- ---------------------------------------------------------------------------

UPDATE app.semantic_view_version_concepts AS m
   SET role = c.kind
  FROM app.semantic_concepts AS c
 WHERE c.id = m.concept_id
   AND m.role <> c.kind;

-- ---------------------------------------------------------------------------
-- 2. The record of each recompilation attempt.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.semantic_recompile_attempts (
    view_version_id     TEXT        NOT NULL
                        REFERENCES app.semantic_view_versions (id) ON DELETE CASCADE,
    compiler_version    TEXT        NOT NULL,
    project_id          TEXT        NOT NULL
                        REFERENCES app.projects (id) ON DELETE CASCADE,
    outcome             TEXT        NOT NULL
                        CHECK (outcome IN ('recompiled', 'refused')),
    -- The refusals of the compilation, in the same shape `prepare` stores under
    -- `validation.refusals`: `[{code, message, path}]`. Empty when recompiled.
    refusals            JSONB       NOT NULL DEFAULT '[]'::jsonb
                        CHECK (jsonb_typeof(refusals) = 'array'),
    -- Where the composition was read: the confirmed change set, or the stored
    -- rows plus the previous artifact's declared inputs.
    composition_source  TEXT        NOT NULL
                        CHECK (composition_source IN ('change_set', 'stored_rows')),
    artifact_id         TEXT
                        REFERENCES app.semantic_compiled_artifacts (id) ON DELETE SET NULL,
    attempted_by        TEXT        NOT NULL,
    attempted_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_semantic_recompile_attempts PRIMARY KEY (view_version_id, compiler_version),
    -- A recompiled attempt names the artifact it wrote; a refused one has none.
    CONSTRAINT ck_semantic_recompile_attempts_artifact
        CHECK ((outcome = 'recompiled') = (artifact_id IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_semantic_recompile_attempts_project
    ON app.semantic_recompile_attempts (project_id, attempted_at DESC);

COMMENT ON TABLE app.semantic_recompile_attempts IS
    'AI-346: the newest attempt to re-derive a compiled artifact under a compiler version. A record of an attempt, not a version -- upserted.';

COMMIT;
