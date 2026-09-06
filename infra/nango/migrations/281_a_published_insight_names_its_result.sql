-- 281 -- A published daily insight names the Result its publication produced (AI-294).
--
-- Design answered 2026-08-17 in docs/product-architecture/proactive-assertions.md:
-- publishing an insight produces a Result, because the alternative -- a second
-- Share path over app.render_snapshots -- is governance.md's parallel-store
-- clause arriving at the Share layer. The publication path derives a governed
-- Query Spec from the insight's card contract, versions it through the EXISTING
-- spec store (app.query_specs / app.query_spec_versions), executes it through
-- the EXISTING governed executor (app.query_execution_attempts ->
-- app.query_results), and records here ONLY the lineage: which spec version,
-- which Result. Nothing analytical is duplicated onto this table -- the Result
-- and its manifest stay the single evidence store.
--
-- When the card cannot be put behind a Query Spec (no published Semantic View
-- covers its members, a metric is not a governed concept, ...), the insight
-- still publishes and `result_unavailable_reason` names the exact missing link,
-- so the surface can say "not shareable, because X" instead of offering a
-- control that fails. A row never carries both a Result and a reason.
--
-- FK style mirrors migration 061's render_snapshot_id: ON DELETE SET NULL --
-- an erased Result (RGPD org erasure) detaches the lineage without deleting
-- the editorial artifact.
--
-- STRICTLY ADDITIVE. IDEMPOTENT.

ALTER TABLE app.daily_insights
    ADD COLUMN IF NOT EXISTS query_spec_version_id TEXT,
    ADD COLUMN IF NOT EXISTS result_id TEXT,
    ADD COLUMN IF NOT EXISTS result_unavailable_reason TEXT;

ALTER TABLE app.daily_insights
    DROP CONSTRAINT IF EXISTS fk_daily_insights_query_spec_version;
ALTER TABLE app.daily_insights
    ADD CONSTRAINT fk_daily_insights_query_spec_version
    FOREIGN KEY (query_spec_version_id) REFERENCES app.query_spec_versions(id)
    ON DELETE SET NULL;

ALTER TABLE app.daily_insights
    DROP CONSTRAINT IF EXISTS fk_daily_insights_result;
ALTER TABLE app.daily_insights
    ADD CONSTRAINT fk_daily_insights_result
    FOREIGN KEY (result_id) REFERENCES app.query_results(id)
    ON DELETE SET NULL;

-- A Result and a reason for its absence cannot both be true of one row.
ALTER TABLE app.daily_insights
    DROP CONSTRAINT IF EXISTS ck_daily_insights_result_or_reason;
ALTER TABLE app.daily_insights
    ADD CONSTRAINT ck_daily_insights_result_or_reason
    CHECK (result_id IS NULL OR result_unavailable_reason IS NULL);
