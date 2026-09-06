-- Story 60.2: a metric declares its additivity, or it is not stored.
--
-- WHY 237 AND NOT 236. The story was drafted against a catalog whose head was
-- 235; `236_a_client_object_kind_names_what_feeds_it.sql` was committed by story
-- 64.1 between the drafting and this application. The identifier was re-measured
-- (`ls infra/nango/migrations/*.sql | wc -l` -> 236) rather than trusted.
--
-- WHAT 142 ALREADY REFUSES, AND THE HOLE IT LEAVES.
--   `142_semantic_model.sql:189-193` (ck_semantic_concept_versions_metric) says a
--   metric carries an expression AND (an aggregation OR additivity_class =
--   'non_additive'). Read the second clause carefully: a metric that declares
--   `aggregation = {"function": "sum"}` satisfies it with `additivity_class`
--   left NULL. That is the hole. The row is legal, silent, and carries no answer
--   to "may this be rolled up across every dimension?".
--
-- WHY THAT HOLE STOPS BEING TOLERABLE IN THIS STORY, PRECISELY.
--   Until now nothing read `additivity_class` except two display surfaces
--   (`governance_read_model.py:756`, `:2527`), so a NULL was merely a blank
--   field on a screen. Story 60.2 makes the RENDER read it
--   (`core.metric_semantics.resolve_declared_additivity`, consulted by
--   `rollup.compute_rollup` and `cards.get_card`). The moment a render path
--   reads a column, a NULL in that column is no longer "unspecified": it is a
--   value someone downstream has to guess, and the guess is made by whoever
--   wrote the fallback rather than by the person who owns the number. This
--   repository has already paid for that shape once today.
--
-- WHAT IT COSTS, MEASURED BEFORE IT WAS WRITTEN.
--   disposable base : app.semantic_concept_versions 13 rows, kind='metric' 9
--   preprod         : app.semantic_concept_versions 13 rows, kind='metric' 9
--   both            : kind='metric' AND additivity_class IS NULL  ->  0 rows
--   No row is rewritten and no backfill is needed, so the constraint is added
--   dry -- no NOT VALID, no VALIDATE CONSTRAINT in a later migration, no window
--   in which the schema states an invariant the data does not meet.
--
-- WHAT IT DOES NOT DO. It does not touch `aggregation`, `non_additive_dimensions`
-- or the 142 constraint (an applied migration is never re-edited). It says one
-- thing: a stored metric version names its additivity class. Which of the three
-- classes is correct stays an application refusal
-- (`semantic_expressions.validate_aggregation`), because only the application
-- can tell `additive` + `average` apart from `additive` + `sum`.

BEGIN;

ALTER TABLE app.semantic_concept_versions
    ADD CONSTRAINT ck_semantic_concept_versions_metric_additivity
    CHECK (kind <> 'metric' OR additivity_class IS NOT NULL);

COMMENT ON CONSTRAINT ck_semantic_concept_versions_metric_additivity
    ON app.semantic_concept_versions IS
    'Story 60.2: a metric version names one of additive / semi_additive / '
    'non_additive. 142 allowed NULL as soon as an aggregation was present; once '
    'the render reads the declared class, NULL becomes a guess made far from the '
    'person who owns the number.';

COMMIT;
