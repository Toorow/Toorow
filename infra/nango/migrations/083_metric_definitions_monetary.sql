-- infra/nango/migrations/083_metric_definitions_monetary.sql
--
-- Story 39.1: Classify monetary metrics platform-wide (Epic 39 -- shared money
-- and timezone modules).
--
-- Adds ONE additive column to app.metric_definitions (migration 049):
--
--     monetary BOOLEAN NOT NULL DEFAULT FALSE
--
-- The SINGLE SOURCE OF TRUTH for "is this canonical metric money?" at the Epic-27
-- semantic layer. Every money rule keys off it instead of re-deciding per connector:
--   * 39.2  micros contract
--   * 39.3  currency provenance / cross-currency refusal engine
--   * 39.4/39.5  FX conversion helper
--   * 39.6  reconciliation
-- and the read-time detector (datamodel._detect_conflicts) uses it to surface a
-- monetary value arriving WITHOUT a source currency as an explicit CURRENCY_GAP
-- rather than a naked amount (E39-FR02, fail-closed).
--
-- WHY A STORED COLUMN, NOT A DERIVED EXPRESSION (Story 39.1 design decision):
--   * `format` is NULL for every migrated PLATFORM default (the dim_metric.csv seed has
--     no format column; parse_dim_metric never sets it) -- deriving monetary from
--     format='currency' would classify EVERY default as non-monetary. `format` is a
--     display hint, it cannot bear a load-bearing money invariant.
--   * `additive`/`aggregation_type` do not separate money from counts: `cost` and
--     `impressions` are both (additive=true, sum). Money is an orthogonal semantic axis.
--   * A stored column resolves through the EXISTING PROJECT > ORG > PLATFORM cascade
--     (resolve_metric_definitions) for free -- a project can, in principle, reclassify a
--     custom metric (E39-NFR04, per-project, no platform hardcode) -- and is queryable
--     platform-wide via a single SELECT (E39-FR01). A derived expression could not be
--     overridden per scope nor audited as a mutation.
-- The classification itself is DECIDED ONCE by a pure function
-- (metric_semantics._classify_monetary), run inside import_platform_defaults to seed the
-- PLATFORM defaults; after that this stored column is authoritative and curatable.
--
-- BACKFILL-FREE: NOT NULL with DEFAULT FALSE -> existing rows become FALSE with no
-- backfill; the seed re-import (import_platform_defaults) then flips the monetary ones
-- (cost/revenue/ad_revenue/all_revenue/conversions_value) to TRUE.
--
-- SCOPE: column-add ONLY. Does NOT touch the scope CHECK, the ratio CHECK, the COALESCE
-- unicity index, or any trigger of migration 049 (049 is APPLIED to Supabase and is
-- immutable -- this is a NEW additive migration, never an edit to 049).
--
-- Apply order: after 082 (highest present at authoring time, 2026-07-23). Coordinate with
-- parallel sessions -- the orchestrator owns the final number and renames if 083 is taken.
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/083_metric_definitions_monetary.sql
--
-- Schema-Change-Checklist (CONTRIBUTING.md):
--   [x] Additive & idempotent (ADD COLUMN IF NOT EXISTS; the whole file is replayable)
--   [x] New column has a default (BOOLEAN NOT NULL DEFAULT FALSE -- backfill-free)
--   [x] No destructive DROP/ALTER on populated columns
-- Verified: highest migration present is 082 (connector_installation_state). This is 083.
--
-- PENDING -- NOT applied to Supabase. Author additive + idempotent; the apply to Supabase
-- is JEAN'S HUMAN GATE (do NOT apply via MCP). Replayable locally as-is.
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- ---------------------------------------------------------------------------
-- Additive column: monetary. Backfill-free NOT NULL DEFAULT FALSE.
-- ---------------------------------------------------------------------------
ALTER TABLE app.metric_definitions
    ADD COLUMN IF NOT EXISTS monetary BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN app.metric_definitions.monetary IS
    'Story 39.1 (Epic 39): is this canonical metric money (cost/revenue/fee/refund + eCPM/CPC/CPA base amounts)? Single source of truth for the money contract -- micros (39.2), currency provenance/refusal (39.3), FX (39.4/39.5), reconciliation (39.6). Classified once at the semantic layer by metric_semantics._classify_monetary, curatable per scope, NOT derived from format (NULL for migrated defaults).';

-- ---------------------------------------------------------------------------
-- Partial index for the platform-wide "which metrics are money?" query (E39-FR01).
-- app.metric_definitions is a SMALL table (one row per canonical metric per scope), so
-- the index is a convenience, not a necessity; it stays cheap because it is PARTIAL
-- (only monetary=TRUE rows) and IF NOT EXISTS keeps the migration replayable.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS ix_metric_definitions_monetary
    ON app.metric_definitions (canonical_name)
    WHERE monetary;

COMMIT;
