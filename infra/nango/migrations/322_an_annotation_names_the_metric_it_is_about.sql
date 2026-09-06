-- 322 -- An annotation names the metric it is about.
--
-- THE MEASUREMENT. `docs/product-architecture/proactive-assertions.md` is
-- incomplete if "a context event is attached to a claim without being scoped to
-- that claim's metric, connector and date". Two of the three are scoped today:
-- `briefing.context_event_walk` and `anomaly_alerts._fetch_context_events_for_
-- anomaly` both join on the claim's EXACT date and disqualify an event whose
-- `platform` contradicts the claim's connector. The third could not be:
--
--     grep -c "metric" infra/nango/migrations/009_create_context_events.sql
--     grep -c "metric" infra/nango/migrations/055_context_events_mmm_cols.sql
--
-- return 0 and 0. `app.context_events` has no metric column, so both walks put
-- `metric` in `unscoped_dimensions` unconditionally, and `candidate_fate
-- .REASON_METRIC_MISMATCH` was declared in the vocabulary and produced by
-- nobody. A price change logged on a day was offered as the candidate cause of
-- an impressions anomaly on that day, because nothing could say it was about
-- cost.
--
-- WHAT THIS ADDS, and why one column rather than a list.
--
--   metric   the governed field this annotation is about, in the SAME vocabulary
--            the claims use: `fact_daily_kpi.metric`, `app.target_fields.name`
--            and a published Semantic Concept name are one namespace, resolved
--            by `core.governed_field_catalogue.governed_names` -- the door
--            `assert_mdm_tags_resolve` already puts a Skill's `mdm_tags`
--            through. Writing a second vocabulary here would mean an event that
--            names `cost` and a claim that names `cost` could fail to compare.
--
-- ONE, not a list: an annotation that names two metrics is two claims about
-- what happened, and a set-valued discriminant would need a rule for "partial
-- overlap" that no ratified document has taken. An event about several metrics
-- is written once per metric, or once with no metric at all -- which is the
-- second half of this decision.
--
-- NULLABLE, AND THAT IS THE POINT -- the same reading migration 262 wrote for
-- the entity pair. An outage, a holiday, a site migration concerns EVERY metric:
-- NULL means "about no metric in particular", and such an event stays admissible
-- under any claim. It never means "about a metric and we lost which". What is
-- refused is the third state: a blank string, which reads as a metric nobody
-- can compare.
--
-- THE MIRROR CARRIES IT WITH NO CODE CHANGE. `mirror_sync` has no curated
-- projection for `context_events`; it syncs `SELECT * FROM app.context_events`
-- (mirror_sync.py, the "otherwise" branch of `_fetch_from_postgres`), so the
-- column lands in `mirror.context_events` on the next sync. The readers
-- intersect their SELECT with `information_schema.columns` and degrade to the
-- narrower join on a mirror that predates this migration -- SAYING that the
-- dimension could not be checked rather than presenting the result as scoped.
--
-- NO BACKFILL, and it is a refusal rather than an omission. Every existing row
-- keeps NULL, which is the honest reading: nobody was ever asked which metric
-- their annotation was about, so deriving one from a label would invent an
-- author's intent. A NULL row stays admissible exactly as it is today, so no
-- pairing that worked yesterday stops working.
--
-- Contract: docs/product-architecture/proactive-assertions.md ("Incomplete if",
-- the metric/connector/date clause) and docs/product-architecture/context-hub.md
-- ("Amendment, 2026-08-30 -- an annotation names the metric it is about").

BEGIN;

ALTER TABLE app.context_events
    ADD COLUMN IF NOT EXISTS metric TEXT;

-- A blank metric is not "every metric" -- it is a name nobody can compare, and
-- it would silently disqualify the event under every claim. Either a name or
-- NULL; checked, not commented.
ALTER TABLE app.context_events
    DROP CONSTRAINT IF EXISTS ck_context_events_metric_named;
ALTER TABLE app.context_events
    ADD CONSTRAINT ck_context_events_metric_named CHECK (
        metric IS NULL OR btrim(metric) <> ''
    );

-- The scoped walks read (project, date) and then discriminate on metric. The
-- partial index carries the rows that CAN be discriminated; a NULL row is
-- admissible everywhere and is already reached by ctx_events_project_date.
CREATE INDEX IF NOT EXISTS ix_context_events_metric
    ON app.context_events (project_id, metric, event_date)
    WHERE metric IS NOT NULL;

COMMENT ON COLUMN app.context_events.metric IS
    'The governed metric this annotation is about, in the vocabulary claims use '
    '(fact_daily_kpi.metric / app.target_fields.name / a published Semantic '
    'Concept name), resolved by core.governed_field_catalogue. NULL means the '
    'event is about no metric in particular -- admissible under every claim -- '
    'never that the metric was lost.';

COMMIT;
