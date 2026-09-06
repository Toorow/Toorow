-- 285: `analysis_reports` and `analysis_notebooks` gain `archived_by`.
--
-- WHY. Migration 154 gave both stable heads an `archived_at` and a partial index
-- (`idx_analysis_reports_live ... WHERE archived_at IS NULL`) that assumed the
-- filter a list would apply. Its own header states the intent: the two heads
-- "may advance their current-version pointer AND BE ARCHIVED". Nothing ever
-- wrote the column. Measured 2026-08-17 (audit reports 02 and 11): zero
-- `SET archived_at` anywhere in the repository, so the refusals that read it --
-- "an archived Report does not accept new versions" -- were unreachable code,
-- and archiving was modelled but inatteignable.
--
-- The gesture lands in the same change as this column. What it needs and 154 did
-- not give it is ATTRIBUTION: `created_by` records who made the artifact, and
-- nothing recorded who retired it.
--
-- THE CLASS, not the instance. `app.datastreams` already carries `archived_by`
-- and its deletion path writes it. An analysis artifact is retired by a person
-- for a reason exactly as a Datastream is, so it answers the same question in
-- the same shape rather than inventing a second one.
--
-- Deliberately NOT added: an `archived_reason`. Nothing asks for one today, and
-- a nullable free-text column nobody fills is how a schema grows fields that
-- read as "the reason was not important" rather than "no reason was asked".

BEGIN;

ALTER TABLE app.analysis_reports
    ADD COLUMN IF NOT EXISTS archived_by TEXT;

ALTER TABLE app.analysis_notebooks
    ADD COLUMN IF NOT EXISTS archived_by TEXT;

-- The pair travels together or the row lies: an artifact archived by nobody, or
-- attributed to someone while still live, are both states the gesture cannot
-- produce and neither should be storable.
ALTER TABLE app.analysis_reports
    ADD CONSTRAINT ck_analysis_reports_archived_pair
    CHECK ((archived_at IS NULL) = (archived_by IS NULL));

ALTER TABLE app.analysis_notebooks
    ADD CONSTRAINT ck_analysis_notebooks_archived_pair
    CHECK ((archived_at IS NULL) = (archived_by IS NULL));

-- The Notebooks list has no live-only index; its list filters archived rows out
-- by default from now on, so it gets the sibling of `idx_analysis_reports_live`.
CREATE INDEX IF NOT EXISTS idx_analysis_notebooks_live
    ON app.analysis_notebooks (project_id, updated_at DESC)
    WHERE archived_at IS NULL;

COMMENT ON COLUMN app.analysis_reports.archived_by IS
    'Who archived this Report. Travels with archived_at (ck_analysis_reports_archived_pair).';
COMMENT ON COLUMN app.analysis_notebooks.archived_by IS
    'Who archived this Notebook. Travels with archived_at (ck_analysis_notebooks_archived_pair).';

COMMIT;
