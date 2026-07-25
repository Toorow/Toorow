-- 096: Test-workspace read surfaces — golden questions + eval runs.
--
-- The Test workspace reads the eval-loop state (Epic 14). The runner
-- (scripts/run_evals.py) is offline; these project-scoped tables hold the
-- benchmark set and the run history the console surfaces:
--   app.golden_questions  -- the versioned benchmark set + last result
--   app.eval_runs         -- history of runs (score, precision, regressions)
-- Idempotent seed for the default project.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.golden_questions (
    id                 TEXT        PRIMARY KEY DEFAULT gen_random_uuid()::text,
    project_id         TEXT        NOT NULL,
    question           TEXT        NOT NULL,
    topic              TEXT        NOT NULL,
    expected_citations INTEGER     NOT NULL DEFAULT 0,
    last_result        TEXT        NOT NULL DEFAULT 'pass',  -- pass | fail
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_golden_questions_project_question UNIQUE (project_id, question)
);
CREATE INDEX IF NOT EXISTS idx_golden_questions_project
    ON app.golden_questions (project_id);

CREATE TABLE IF NOT EXISTS app.eval_runs (
    id            TEXT        PRIMARY KEY DEFAULT gen_random_uuid()::text,
    project_id    TEXT        NOT NULL,
    run_at        TIMESTAMPTZ NOT NULL,
    score_passed  INTEGER     NOT NULL,
    score_total   INTEGER     NOT NULL,
    precision_pct NUMERIC(5,2) NOT NULL,
    regressions   INTEGER     NOT NULL DEFAULT 0,
    status        TEXT        NOT NULL,  -- passed | regressed
    CONSTRAINT uq_eval_runs_project_runat UNIQUE (project_id, run_at)
);
CREATE INDEX IF NOT EXISTS idx_eval_runs_project
    ON app.eval_runs (project_id);

-- Seed — default project.
INSERT INTO app.golden_questions (project_id, question, topic, expected_citations, last_result) VALUES
    ('default', 'What was total paid-media spend last month across all channels?', 'Spend', 3, 'pass'),
    ('default', 'Which campaign had the highest ROAS in Q2?', 'Performance', 2, 'pass'),
    ('default', 'How did organic search clicks trend over the last 8 weeks?', 'Organic search', 2, 'fail'),
    ('default', 'What share of conversions is attributed to Meta Ads post-click?', 'Attribution', 4, 'pass'),
    ('default', 'Break down revenue by country for the current quarter.', 'Geography', 3, 'pass'),
    ('default', 'Is reported spend consistent between Google Ads and GA4?', 'Data quality', 2, 'fail'),
    ('default', 'What is the blended CPA across paid channels this month?', 'Performance', 3, 'pass'),
    ('default', 'Which datastreams are missing data for yesterday?', 'Data quality', 1, 'pass')
ON CONFLICT (project_id, question) DO NOTHING;

INSERT INTO app.eval_runs (project_id, run_at, score_passed, score_total, precision_pct, regressions, status) VALUES
    ('default', '2026-07-24 09:12:00+00', 48, 50, 96.00, 2, 'regressed'),
    ('default', '2026-07-23 18:40:00+00', 50, 50, 100.00, 0, 'passed'),
    ('default', '2026-07-23 08:55:00+00', 49, 50, 98.00, 0, 'passed'),
    ('default', '2026-07-22 17:03:00+00', 46, 50, 92.00, 3, 'regressed'),
    ('default', '2026-07-22 09:20:00+00', 49, 50, 98.00, 0, 'passed'),
    ('default', '2026-07-21 16:48:00+00', 50, 50, 100.00, 0, 'passed')
ON CONFLICT (project_id, run_at) DO NOTHING;
