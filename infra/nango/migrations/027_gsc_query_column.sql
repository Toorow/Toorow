-- infra/nango/migrations/027_gsc_query_column.sql
--
-- Story 10.5 (Keywords: GSC cannibalisation flag), AC 9.
--
-- Adds a nullable ``query`` column to the raw GSC landing table so the new
-- ``query_page_daily`` report profile can land (query, page) rows alongside the
-- existing page/country/device rows. ADDITIVE and NULLABLE: existing rows keep a
-- NULL query and are naturally excluded by ``stg_gsc_query_page_daily`` (which
-- filters ``query IS NOT NULL``); no destructive ALTER, no default required, no
-- change to any existing staging model or mart block (Schema Change Checklist AI-29).
--
-- CORRECTION (AI-53, code over story): the story text referenced 022, but 022 is
-- already taken (022_pull_jobs_dedup_index.sql) and the highest existing migration
-- is 026 -- this migration is therefore numbered 027.
--
-- The raw landing table is created by the GSC connector's _RAW_CREATE_DDL (DuckDB
-- warehouse at P3-dev; the same column is added there additively). This SQL is the
-- warehouse-migration companion for environments where raw_gsc_daily is a managed
-- table rather than connector-created.
--
-- Apply with:
--   psql $PLATFORM_DB_URL -f infra/nango/migrations/027_gsc_query_column.sql

-- Conditionnel: raw_gsc_daily n'est PAS creee par le schema app, c'est le
-- connecteur GSC qui la cree dans l'entrepot (_RAW_CREATE_DDL). Sur une base
-- fraiche -- le job CI isolation qui rejoue toutes les migrations dans l'ordre --
-- la table n'existe pas et un ALTER sec echoue. Le no-op est exactement
-- l'intention decrite plus haut: n'agir que la ou la table est managee.
DO $$
BEGIN
  IF to_regclass('public.raw_gsc_daily') IS NOT NULL THEN
    EXECUTE 'ALTER TABLE raw_gsc_daily ADD COLUMN IF NOT EXISTS query VARCHAR';
  END IF;
END
$$;
