-- infra/nango/migrations/033_projection_governance_prefs.sql
--
-- Story 12.4: governed, project-scoped cardinality/scan thresholds for the safe
-- KPI projection compile gate. Additive and idempotent after migration 032 (the
-- highest applied migration: 030 12.2, 031 11.1, 032 12.3). 033 is the next free
-- integer; confirm 032 is still the highest at merge and renumber if others land.
--
-- WHY project_preferences (not a new table): 12.4 adds NO candidate/full-grain
-- metadata table. The full-grain relation and the safe projection are pure dbt
-- models derived from the immutable 12.3 mapping version (AD-8: dbt is the only
-- mart writer), so no Postgres candidate registry is required in 12.4 (that
-- linkage, if any, is 12.5's atomic-publication concern -- Open Question 2). The
-- ONLY Postgres state 12.4 needs is the governed threshold, and per the
-- "prefer per-project preferences" rule it lives on the existing per-project
-- app.project_preferences row -- NEVER a platform-wide hardcode.
--
-- Columns (both NULL-able, additive, no destructive default):
--   max_projection_grain_cardinality : BIGINT NULL -- max distinct-grain rows the
--       safe projection may materialize before the compile gate BLOCKS or
--       requires explicit approval. NULL => the documented default
--       (DEFAULT_MAX_GRAIN_CARDINALITY = 1_000_000 in datastream_projection.py);
--       the compiled plan records threshold_source='documented_default' so the
--       fallback is never silent.
--   max_projection_scan_bytes        : BIGINT NULL -- max estimated scanned bytes
--       before the gate BLOCKS/requires approval. NULL => documented default
--       (DEFAULT_MAX_SCAN_BYTES = 10 GiB). Metadata-based estimate only in 12.4
--       (no live BigQuery dry_run -- AI-13 N/A; the live byte estimate is
--       deferred to the story that first writes BigQuery).
--
-- Invariants:
--   AD-8  : Postgres is the sole writer; DuckDB/BigQuery reads via the governed
--           mirror (mirror_sync.py). These columns propagate automatically via the
--           existing SELECT * FROM app.project_preferences already in place.
--   AD-9  : over-limit is surfaced honestly (estimate + expensive field named);
--           no dimension is silently dropped or top-N-capped to fit under a limit.
--   AI-44 : file named 033_projection_governance_prefs.sql (zero-padded prefix).
--
-- Idempotent: ADD COLUMN IF NOT EXISTS + guarded CHECK (DROP IF EXISTS before ADD).
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/033_projection_governance_prefs.sql

BEGIN;

ALTER TABLE app.project_preferences
    ADD COLUMN IF NOT EXISTS max_projection_grain_cardinality BIGINT;

ALTER TABLE app.project_preferences
    ADD COLUMN IF NOT EXISTS max_projection_scan_bytes BIGINT;

-- Positive-or-NULL guards: a zero/negative governed limit is nonsensical (it
-- would block every projection). NULL means "use the documented default".
ALTER TABLE app.project_preferences
    DROP CONSTRAINT IF EXISTS chk_project_preferences_max_grain_cardinality;
ALTER TABLE app.project_preferences
    ADD CONSTRAINT chk_project_preferences_max_grain_cardinality
    CHECK (
        max_projection_grain_cardinality IS NULL
        OR max_projection_grain_cardinality > 0
    );

ALTER TABLE app.project_preferences
    DROP CONSTRAINT IF EXISTS chk_project_preferences_max_scan_bytes;
ALTER TABLE app.project_preferences
    ADD CONSTRAINT chk_project_preferences_max_scan_bytes
    CHECK (
        max_projection_scan_bytes IS NULL
        OR max_projection_scan_bytes > 0
    );

COMMIT;
