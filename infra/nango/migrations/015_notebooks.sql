-- Migration 015: app.notebooks and app.notebook_runs (Story 6.5, AC1)
--
-- Notebooks: living documents that re-run a named report with a relative window rule.
-- Notebook runs: immutable history of each execution with pull_ids snapshot (provenance proof).
--
-- ID prefixes (ARCHITECTURE-SPINE §Consistency Conventions):
--   nb_    -> app.notebooks   (notebook definition)
--   nbrun_ -> app.notebook_runs (individual run record)
--
-- envelope_inline size gate (512KB):
--   Store the AD-1 envelope inline as JSONB only when pg_column_size < 512KB.
--   Larger envelopes: set envelope_ref='deferred' and leave envelope_inline NULL.
--   For P0-scope (≤500 rows / ≤30 metrics), inline storage is correct and sufficient.
--   Actual blob storage (GCS) is an Epic 7 concern; envelope_ref='deferred' is a placeholder.
--
-- Verified: last migration was 014 (Story 6.1). This is 015.
BEGIN;

CREATE TABLE IF NOT EXISTS app.notebooks (
    id               TEXT        PRIMARY KEY,           -- prefixed ULID: 'nb_'
    project_id       TEXT        NOT NULL,              -- project identifier (TEXT, no FK — app.projects table not present at P0)
    title            TEXT        NOT NULL,
    report_ref       TEXT        NOT NULL,              -- e.g. 'gsc/position_movements' or 'adhoc'
    window_rule      TEXT        NOT NULL,              -- relative: 'last_30d', 'last_7d', 'last_90d'
    narrative_prompt TEXT,                             -- override prompt; NULL = use report definition's prompt
    created_by       TEXT        NOT NULL,              -- identity subject from AD-14
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS notebooks_project ON app.notebooks (project_id);

CREATE TABLE IF NOT EXISTS app.notebook_runs (
    id              TEXT        PRIMARY KEY,           -- prefixed ULID: 'nbrun_'
    notebook_id     TEXT        NOT NULL REFERENCES app.notebooks(id) ON DELETE CASCADE,
    executed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    as_of           TEXT,                              -- ISO date for as-of queries; NULL = current
    summary_text    TEXT        NOT NULL,              -- the built narrative (<=30 lines, citation-formatted)
    envelope_ref    TEXT,                              -- nullable: future blob ref if envelope stored externally
    envelope_inline JSONB,                             -- inline structuredContent envelope snapshot (size-gated)
    pull_ids        TEXT[]      NOT NULL DEFAULT '{}', -- pull_ids resolved at run time (proves re-resolution)
    status          TEXT        NOT NULL DEFAULT 'success' CHECK (status IN ('success', 'error')),
    error_message   TEXT                               -- NULL on success
);
CREATE INDEX IF NOT EXISTS notebook_runs_notebook ON app.notebook_runs (notebook_id);
CREATE INDEX IF NOT EXISTS notebook_runs_executed ON app.notebook_runs (executed_at DESC);

COMMIT;
