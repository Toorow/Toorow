-- How a plan-line <-> campaign match was OBTAINED, on the match itself.
-- Story 61.3, arbitrage A1 (a).
--
-- WHY TWO COLUMNS AND NOT A TABLE. `app.plan_line_mappings` (migration 041)
-- carries `connector`, `campaign_ref`, `split_weight`, `status` and `created_by`
-- -- WHO made the match and WHEN, never HOW. So an equality of codes and a
-- difflib resemblance at 0.89 are the same row, while the pacing built on top of
-- them does not deserve the same confidence. The level belongs to the match: a
-- proposal table beside it would be a second store describing one object, and the
-- day the two disagreed neither would be the authority.
--
-- THE VOCABULARY IS BORROWED, NOT INVENTED -- and this is its fourth reuse in
-- this batch. `exact | normalized | similarity | manual` is the CHECK migration
-- 052 already writes on `app.dimension_value_mappings.suggestion_method`, and
-- `server/core/dimension_conformance.py:83-86` is where the four constants live.
-- `server/core/plan_mapping_suggest.py` imports them rather than spelling them
-- again, and this file mirrors the same CHECK so the database refuses a fifth
-- word in both places.
--
-- TWO TIERS OF THE PLAN DO NOT EXIST, AND SAYING SO IS PART OF THE MIGRATION.
-- `epic-61-placement-mapping-workbench.md:41` names four tiers: "Code exact ->
-- regle regex -> proposition IA floue -> association manuelle". Measured
-- 2026-08-09:
--
--   * there is NO regex-rule tier. The neighbour of `exact` is `normalized`, and
--     `dimension_conformance.normalize_value` is a FIXED pipeline (bracket tags,
--     datestamps, diacritics, case, separators, affixes) that no client writes.
--     The "ordered matching rules" two ratified documents place in Governance
--     (`docs/product-architecture/capabilities/placement-mapping.md`,
--     `docs/product-architecture/project-settings.md`) are carried by no table,
--     no module and no route -- `grep -rn "ordered matching rules" docs/` returns
--     2 documents and 0 code -- and this migration does not open that store;
--   * there is NO AI tier. `similarity` is `difflib.SequenceMatcher` at the 0.88
--     threshold of `dimension_conformance.DEFAULT_SIMILARITY_THRESHOLD`. No
--     model, no network call. The word `AI` is refused on the screen and in the
--     payload: naming a string comparison an intelligence is a promise the code
--     does not keep.
--
-- NULL IS AN ABSENCE AND NEVER `manual`. The 49 matches these two databases
-- carried when this file was written were all written before it, so they have no
-- recorded level. Defaulting them to `manual` would state that a person typed
-- them, which nothing measured; they read as "level not recorded", which is what
-- is true. That is also why neither column is NOT NULL and why neither carries a
-- DEFAULT.
--
-- WHAT THIS FILE ENFORCES, AND NOTHING MORE -- stated so no reader trusts a guard
-- that is not here:
--
--   * the method is one of the four words, or absent;
--   * the score, when present, lies in [0,1] -- the same bound migration 052 puts
--     on `suggestion_score`;
--   * a `manual` match carries no score. There is nothing to measure about a
--     match somebody typed, and a `1.0` beside it would read as a computed
--     certainty. The screen prints a score for `similarity` alone, because
--     `exact` and `normalized` score 1.0 BY CONSTRUCTION
--     (`plan_mapping_suggest.py:180`, `:126`) and printing that everywhere would
--     make a tautology look like a measurement.
--
-- Nothing below checks that the stored method is the one the engine would compute
-- again today: a campaign renamed after the match would change the answer, and a
-- level is a record of how the match was made, not a claim about the present.
-- `core/datastream_workbench_api.py` re-derives the level from the engine at the
-- moment of the confirmation instead of accepting it from a caller, which is
-- where that guard lives.
--
-- ERASURE: this file adds no table and no foreign key, so it changes nothing
-- about what `core.org_purge` reaches. `app.plan_line_mappings` was already on
-- its plan through the ON DELETE RESTRICT edge migration 041 declared.

BEGIN;

ALTER TABLE app.plan_line_mappings
    ADD COLUMN IF NOT EXISTS match_method TEXT;
ALTER TABLE app.plan_line_mappings
    ADD COLUMN IF NOT EXISTS match_score  DOUBLE PRECISION;

-- Named constraints, dropped first: an inline CHECK on ADD COLUMN would be given
-- a generated name, and a replay of this file could not find it again.
ALTER TABLE app.plan_line_mappings
    DROP CONSTRAINT IF EXISTS ck_plan_line_mappings_match_method;
ALTER TABLE app.plan_line_mappings
    ADD CONSTRAINT ck_plan_line_mappings_match_method CHECK (
        match_method IS NULL
        OR match_method IN ('exact', 'normalized', 'similarity', 'manual')
    );

ALTER TABLE app.plan_line_mappings
    DROP CONSTRAINT IF EXISTS ck_plan_line_mappings_match_score;
ALTER TABLE app.plan_line_mappings
    ADD CONSTRAINT ck_plan_line_mappings_match_score CHECK (
        match_score IS NULL OR (match_score >= 0 AND match_score <= 1)
    );

ALTER TABLE app.plan_line_mappings
    DROP CONSTRAINT IF EXISTS ck_plan_line_mappings_manual_carries_no_score;
ALTER TABLE app.plan_line_mappings
    ADD CONSTRAINT ck_plan_line_mappings_manual_carries_no_score CHECK (
        match_method IS DISTINCT FROM 'manual' OR match_score IS NULL
    );

COMMENT ON COLUMN app.plan_line_mappings.match_method IS
    'Story 61.3: HOW this match was obtained -- exact | normalized | similarity | manual, the '
    'vocabulary migration 052 writes on app.dimension_value_mappings.suggestion_method and '
    'server/core/dimension_conformance.py:83-86 declares. NULL means the level was never '
    'recorded (every match written before this migration), which the surfaces render as an '
    'absence and never as ''manual''. There is no regex-rule tier and no AI tier: the neighbour '
    'of ''exact'' is a FIXED normalisation pipeline, and ''similarity'' is difflib at 0.88.';
COMMENT ON COLUMN app.plan_line_mappings.match_score IS
    'Story 61.3: the difflib ratio of a ''similarity'' match, in [0,1]. 1.0 for ''exact'' and '
    '''normalized'', which score 1.0 by construction and therefore print no number on any '
    'screen. NULL on ''manual'' (enforced): a match somebody typed has nothing to measure, and '
    'a number beside it would read as a computed certainty.';

COMMIT;
