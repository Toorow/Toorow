-- 335: story 72.6 -- a Visualization Spec version materialised from a Chart
-- Template says which template version it came from, and says it as PROVENANCE.
--
-- WHY A COLUMN AT ALL, AND WHY THIS ONE. Measured before writing this file:
--
--     grep -rn "provenance" server/core/visualization_specs.py
--     #   only `_provenance_hint`, which projects
--     #   app.semantic_concept_versions.provenance for the Builder's left rail
--     grep -rn "derived_from\|template_version_id" infra/nango/migrations/156*.sql
--     #   (nothing)
--
-- So `app.visualization_spec_versions` had nowhere to record where a version came
-- from, and `app.visualizations` had no origin column either -- unlike
-- `app.analysis_reports` (154:98-127) and `app.visualization_templates`
-- (333:98-134), which both carry their seed columns. The ratified amendment of
-- `visualization-and-rendering.md` (2026-08-31) asks the Chart Template workbench
-- Overview to show "what uses it", and story 72.6's AC23 asks for the template's
-- provenance to be recorded AS PROVENANCE. Without this column the only honest
-- answer to "which visualizations came from this template" is "nobody recorded
-- it".
--
-- WHY IT IS NOT INSIDE `spec`. AC23: the materialised document must be
-- INDISTINGUISHABLE from one a person composed in the Builder -- same validator,
-- same `content_hash`, same table, same refusals. `content_hash` covers the
-- whole `spec` document (`visualization_specs.canonical_hash`), so a provenance
-- key inside it would make two identical presentations hash differently
-- depending on how they were born. Provenance is recorded BESIDE the document,
-- never inside it, and no validator reads this column.
--
-- WHY IT IS NULLABLE, AND STAYS NULLABLE. Every version written by hand through
-- the Builder came from no template, and that is the ordinary case. A NOT NULL
-- column with a sentinel would be a template id that resolves to nothing --
-- exactly the fabricated pin migration 333 spent its length refusing.
--
-- WHY THE KEY IS COMPOSITE. `uq_visualization_template_versions_scope`
-- (333) exists for this: a Visualization Spec version of one Project cannot claim
-- provenance from a Chart Template version of another, even when the application
-- layer is wrong. The same rule migration 154 states for every one of its arrows.
--
-- WHAT AN EDIT RECORDS. Nothing. Version N+1 of a Visualization derived from a
-- template is a hand edit, so its `materialized_from_template_version_id` is
-- NULL and its `predecessor_version_id` names the version that WAS materialised.
-- Carrying the template id forward would say a template produced a document it
-- never produced, and the ratified criterion is explicit that editing a
-- derivative never mutates -- nor re-attributes -- the seed.
--
-- MEASURED BEFORE WRITING (disposable cluster on port 55432, 334 rows in
-- `toorow_meta.schema_migrations`, so ledger head 334):
--
--     SELECT count(*) FROM app.visualization_spec_versions;     -- 12
--     SELECT count(*) FROM app.visualization_template_versions; -- 678
--
-- Those are the leftovers of local suites, not production rows, and they are the
-- reason this file only ADDS: a column added NULL on every existing row leaves
-- the new foreign key trivially satisfied (a NULL member of a composite key
-- skips the check under MATCH SIMPLE), so the ALTER validates whatever the table
-- already holds. Nothing is dropped, weakened or re-edited. Not applied to
-- production by this session.

BEGIN;

ALTER TABLE app.visualization_spec_versions
    ADD COLUMN IF NOT EXISTS materialized_from_template_version_id TEXT;

ALTER TABLE app.visualization_spec_versions
    DROP CONSTRAINT IF EXISTS fk_visualization_spec_versions_template_version;
ALTER TABLE app.visualization_spec_versions
    ADD CONSTRAINT fk_visualization_spec_versions_template_version
    FOREIGN KEY (materialized_from_template_version_id, org_id, project_id)
    REFERENCES app.visualization_template_versions (id, org_id, project_id);

COMMENT ON COLUMN app.visualization_spec_versions.materialized_from_template_version_id IS
    'Provenance, never identity: the Chart Template version story 72.6 '
    'materialised this Visualization Spec version from, or NULL when a person '
    'composed it in the Builder. No validator reads it, it is not covered by '
    'content_hash, and a hand edit of a derived visualization records NULL -- '
    'its lineage to the template is its predecessor.';

-- What the Chart Template workbench Overview answers with ("what uses it").
-- Partial, because the ordinary version came from no template and indexing its
-- NULL would be an index of the whole table.
CREATE INDEX IF NOT EXISTS idx_visualization_spec_versions_from_template
    ON app.visualization_spec_versions (materialized_from_template_version_id)
    WHERE materialized_from_template_version_id IS NOT NULL;

COMMIT;
