BEGIN;
CREATE TABLE IF NOT EXISTS app.feedback (
    id              TEXT        PRIMARY KEY,          -- prefixed ULID: 'fb_'
    project_id      TEXT        NOT NULL,
    trace_id        TEXT,                            -- OTel trace_id (NULL when TRACING_ENABLED=false)
    report_ref      TEXT,                            -- e.g. "get_daily_report:2026-07-11" (module:date)
    module          TEXT,                            -- e.g. "google-analytics", "meta-ads", "core"
    rating          INTEGER     NOT NULL,            -- 1 = thumbs up, -1 = thumbs down
    comment         TEXT,                            -- optional qualitative text
    created_by      TEXT        NOT NULL,            -- identity from access_token (AD-14)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS feedback_project ON app.feedback (project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS feedback_trace   ON app.feedback (trace_id) WHERE trace_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS feedback_module  ON app.feedback (project_id, module, created_at DESC);
COMMIT;
