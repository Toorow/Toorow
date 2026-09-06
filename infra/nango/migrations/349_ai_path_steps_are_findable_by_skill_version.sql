-- 349 -- the steps a Skill version crossed are findable without scanning every step.
--
-- `ai_paths.previous_walks` (2026-09-05) serves, with each Skill, the last walks
-- of the Project that crossed one of its steps: `skill_version_id LIKE 'proc_x@%'`.
-- Migration 150 indexes steps by path and by owner, not by Skill version, so that
-- read scanned every step of the Project on every `get_procedure` (Opus review of
-- efe127b7, finding 5). A prefix index serves the LIKE.
CREATE INDEX IF NOT EXISTS idx_ai_path_steps_skill_version
    ON app.ai_path_steps (path_id, skill_version_id text_pattern_ops)
    WHERE skill_version_id IS NOT NULL;
