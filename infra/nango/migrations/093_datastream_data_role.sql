-- infra/nango/migrations/093_datastream_data_role.sql
--
-- Epic 42 (Data overview fleet): give a Datastream a business DATA ROLE so the
-- fleet "Data type" column and grouping stop being UI literals. This is the one
-- genuinely missing field the read-model never carried (source_kind is an
-- ingestion mechanism {connector_pull, external_bq, managed_feed}, NOT a role).
--
-- Taxonomy (IA v2 business roles): Spend, Performance, Revenue & conversions,
-- Forecast & plan, Context, Reference & targets, Operational. NULL is allowed
-- (unclassified) so the column is non-breaking and per-project editable later.
--
-- Additive + idempotent: adds a nullable column (IF NOT EXISTS), a permissive
-- CHECK, and a one-time best-effort backfill from module_name/source_kind for
-- rows that are still NULL. No existing value is overwritten.

BEGIN;

ALTER TABLE app.datastreams
    ADD COLUMN IF NOT EXISTS data_role TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'datastreams_data_role_check'
    ) THEN
        ALTER TABLE app.datastreams
            ADD CONSTRAINT datastreams_data_role_check
            CHECK (data_role IS NULL OR data_role IN (
                'Spend', 'Performance', 'Revenue & conversions',
                'Forecast & plan', 'Context', 'Reference & targets', 'Operational'
            ));
    END IF;
END $$;

-- Best-effort backfill: only touches rows where data_role is still NULL.
UPDATE app.datastreams
   SET data_role = CASE
        WHEN lower(coalesce(module_name, '') || ' ' || coalesce(source_kind, ''))
             ~ '(meta|facebook|google[_-]?ads|googleads|tiktok|linkedin|dv360|dsp|amazon[_-]?ads|pinterest|taboola|microsoft[_-]?ads|ad ?manager|paid)'
            THEN 'Spend'
        WHEN lower(coalesce(module_name, '') || ' ' || coalesce(source_kind, ''))
             ~ '(analytics|ga4|gsc|search[_-]?console|adobe|youtube)'
            THEN 'Context'
        WHEN lower(coalesce(module_name, '') || ' ' || coalesce(source_kind, ''))
             ~ '(sheet|managed[_-]?feed|file|plan|excel|csv)'
            THEN 'Forecast & plan'
        WHEN lower(coalesce(module_name, '') || ' ' || coalesce(source_kind, ''))
             ~ '(stripe|shopify|woocommerce|square|revenue|billing|commerce)'
            THEN 'Revenue & conversions'
        ELSE 'Operational'
    END
 WHERE data_role IS NULL;

COMMIT;
