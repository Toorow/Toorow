-- infra/nango/migrations/026_pull_verifications_rejected_rows.sql
--
-- review-epic-8 #7 completion: the dq_date_format monitor (Story 8.10) reads
-- SUM(pull_verifications.rejected_rows) for yesterday's pulls, but no migration
-- ever created that column. Without it the monitor query raises (caught -> the
-- monitor silently returns False, i.e. stays dead exactly like the original
-- regex-on-normalized-rows version it replaced).
--
-- Add the column additively (default 0). The generic connector's pull() reports
-- a rejected_rows count for unparseable dates; queue._execute_job now threads
-- that count into run_post_pull_verification, which writes it here. Modules that
-- do not reject rows write 0 and never trip the monitor.
--
-- Apply with:
--   psql $PLATFORM_DB_URL -f infra/nango/migrations/026_pull_verifications_rejected_rows.sql

ALTER TABLE app.pull_verifications
    ADD COLUMN IF NOT EXISTS rejected_rows INTEGER NOT NULL DEFAULT 0;
