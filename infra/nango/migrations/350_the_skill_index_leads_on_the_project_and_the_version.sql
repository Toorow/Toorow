-- 350 -- the steps a Skill version crossed are findable per Project, by an escaped prefix.
--
-- Migration 349 indexed (path_id, skill_version_id): the read drove from the paths
-- and probed every step; and `LIKE 'proc_x@%'` extracted the prefix `proc` because
-- `_` is a wildcard, so the pattern-ops half filtered nothing (Opus review of
-- 368f910f, finding 5, measured on 360 000 steps). `ai_paths.previous_walks` now
-- drives from the Project's pinned steps with an ESCAPED prefix; this is its index.
-- 349 is applied and immutable; its index is dropped here, by the next migration.
DROP INDEX IF EXISTS app.idx_ai_path_steps_skill_version;
CREATE INDEX IF NOT EXISTS idx_ai_path_steps_project_skill_version
    ON app.ai_path_steps (project_id, skill_version_id text_pattern_ops)
    WHERE skill_version_id IS NOT NULL;
