-- 266 -- Five applied headers name the wrong eraser. This one names the right
-- one, for the tables they created (AI-263).
--
-- IT CHANGES NO SCHEMA, AND THAT IS THE POINT. A migration header is where this
-- repository records what the database guarantees -- `test_migration_erasure_claims`
-- exists precisely because three migrations in a row wrote a false sentence
-- there and nothing could see it. Five older ones carry the same fault and
-- CANNOT be corrected: `apply_migrations.py` raises `checksum drift: applied
-- migration changed` on any edit to an applied file, and all five are applied on
-- preprod. A forward migration is the only place left to say the true thing.
--
-- WHAT THEY GET WRONG. Each claims, in one wording or another, that
-- `core.org_purge` reaches its rows through the foreign-key graph:
--
--   105_language_dimension_family.sql   « l'effacement d'org passe par la FK,
--                                       decouverte par le graphe de org_purge.py »
--   106_dimension_labels.sql            the same sentence, over `dimension_labels`
--   107_target_fields_versions.sql      a DENIAL, correct but in the old wording
--   119_fee_tax_alignment.sql           « the ON DELETE CASCADE below is picked up
--                                       with no registration »
--   146_tax_fee_governed_ladder.sql     « org_purge walks the FK graph to reach
--                                       them » -- about `fx_rate_observations`,
--                                       a table 146 does not even create
--
-- `plan_purge` walks the graph through `confdeltype IN ('a','r')` ONLY
-- (`org_purge.py:97`) -- NO ACTION and RESTRICT. A CASCADE edge is deliberately
-- absent because Postgres already does the work. So a header that credits the
-- graph for a CASCADE-only table sends the next reader to `core/org_purge.py`,
-- where they will grep for the table, find nothing, and conclude the rows are
-- never erased. They ARE erased. The mechanism named is simply not the one.
--
-- THE MEASUREMENT, taken 2026-08-16 rather than repeated from the older files:
--
--   plan_purge(conn, 'org_EXAMPLE')  ->  3 082 statements
--
--   app.dimension_field_bindings     in the plan: NO   FK delete types: c
--   app.dimension_labels             in the plan: NO   FK delete types: c
--   app.target_fields_versions       in the plan: NO   FK delete types: c
--   app.fee_tax_rules                in the plan: NO   FK delete types: c
--   app.datastream_source_types      in the plan: NO   FK delete types: c
--   app.fx_rate_observations         in the plan: NO   FK delete types: c
--
-- Every one of them is CASCADE-only, and none appears in a single statement of
-- the plan. The erasure is real and it is Postgres's, through the cascade.
--
-- THE CANONICAL DENIAL, in the words migration 235 established, so a reader
-- comparing two headers is comparing two statements of the same shape:
--
--   It is NOT `core.org_purge` that reaches them.
--
-- WHAT THIS DOES NOT CLAIM. It does not say those tables are unreachable, nor
-- that their erasure is at risk: `test_immutability_triggers_yield_to_erasure`
-- (2026-08-16) proves separately that every DELETE guard inside the org tree
-- yields to a flagged erasure. This corrects an ADDRESS, not a guarantee.
--
-- ERASURE, for this migration itself: it creates no table, so `core.org_purge`
-- has nothing here to reach.

BEGIN;

-- The record lives in the header above. The statement below exists so the
-- correction is an APPLIED fact with a checksum rather than a comment in a file
-- nobody has to run -- the same reason the ledger records every other one.
DO $migration$
BEGIN
    RAISE NOTICE
        'AI-263: headers of 105, 106, 107, 119 and 146 name core.org_purge for '
        'CASCADE-only tables. It is NOT core.org_purge that reaches them; '
        'Postgres does, through ON DELETE CASCADE. Measured 2026-08-16: 0 of '
        '3082 plan statements name any of them.';
END
$migration$;

COMMIT;
