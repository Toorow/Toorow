BEGIN;

CREATE TABLE IF NOT EXISTS app.morning_briefings (
    id              TEXT        PRIMARY KEY,           -- prefixed ULID: 'brief_'
    project_id      TEXT        NOT NULL,                             -- project identifier (TEXT, no FK — app.projects table not present at P0)
    briefing_date   DATE        NOT NULL,              -- the date this briefing covers (today's date at build time)
    insights        JSONB       NOT NULL,              -- top insights with provenance (see AC2 for shape)
    built_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    nightly_run_id  TEXT,                              -- identifier of the nightly run that produced this (nullable)
    UNIQUE (project_id, briefing_date)                 -- one briefing per project per day
);
CREATE INDEX IF NOT EXISTS briefings_project_date ON app.morning_briefings (project_id, briefing_date DESC);

COMMIT;
