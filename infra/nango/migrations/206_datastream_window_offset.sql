-- infra/nango/migrations/206_datastream_window_offset.sql
--
-- AI-145: Add configurable extraction window offset (window_offset_days) to app.datastreams.
--
-- Defaults to 1 (window ends at J-1 "yesterday").
-- Supports values 1..90 days offset to accommodate sources with delayed availability (e.g. Search Console).
--

BEGIN;

ALTER TABLE app.datastreams
    ADD COLUMN IF NOT EXISTS window_offset_days INT NOT NULL DEFAULT 1
    CHECK (window_offset_days >= 1 AND window_offset_days <= 90);

COMMIT;
