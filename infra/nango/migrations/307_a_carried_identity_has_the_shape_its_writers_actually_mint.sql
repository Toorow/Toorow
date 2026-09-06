-- 307 -- the carried Business-Domain identity had a shape no writer mints.
--
-- WHAT MIGRATION 143 WROTE, AND WHY IT NEVER FAILED
--
--     ALTER TABLE app.master_data_nodes ADD CONSTRAINT master_data_nodes_id_check
--     CHECK (id ~ '^(mdnode|bd|bcl)_[0-9A-HJKMNP-TV-Z]{26}$');
--
-- Its own comment says what it is for: *"a preserved bd_/bcl_ id carried over
-- from Story 45.1 so that every consumer pinning it keeps resolving"*. It is
-- wrong twice, and it stayed wrong for twenty-six days because 143 also said,
-- in the same section, that it was moving no row. A constraint that guards a
-- path nobody walks is a constraint nobody can find a hole in.
--
-- MEASURED 2026-08-25, on a base at 306 migrations, before this file was written:
--
--   SELECT count(*), left(id,4) FROM app.mdm_business_domains GROUP BY 2;
--     846  bdm_        <- the prefix the writers ACTUALLY mint
--      22  bd_0        <- the only shape 143 anticipated
--   SELECT count(*), left(id,4) FROM app.mdm_business_classifications GROUP BY 2;
--       6  bcl_
--
--   business_taxonomy.py:415   `_mint("bdm")`               -> bdm_<ULID>
--   130_business_taxonomy_context_paths.sql:187
--       'bdm_' || substr(md5(org || ':' || slug), 1, 26)    -> bdm_<26 lowercase hex>
--
-- So the two live writers of a Business Domain id -- the Python one and the
-- organization seed trigger that gives every new organization its six starter
-- domains -- both produce `bdm_`, and one of them produces a HEX body, which the
-- Crockford class `[0-9A-HJKMNP-TV-Z]` cannot match either. Carrying a real
-- Business Domain into the authority was impossible, and the failure it produced
-- named nothing useful:
--
--   CheckViolation: new row for relation "master_data_nodes" violates check
--   constraint "master_data_nodes_id_check"
--   DETAIL: (bdm_3064cb2f22431a4508e126de08, ...)
--
-- WHAT THIS CHANGES, AND WHAT IT REFUSES TO CHANGE
--
-- The set is now the one the repository's writers actually mint, and nothing
-- wider. `bd_` STAYS: 22 rows carry it on the measured base, and dropping a
-- prefix that live rows use in order to tidy a regex is how a convergence
-- becomes unable to carry the very identities it exists for.
--
-- The alternative was to renumber the taxonomy so it fits 143's guess. That is
-- the fork this whole story exists to avoid: a Business Domain id is pinned by
-- Context paths, Datastream links, Project applicability and golden questions,
-- and none of those would follow.
--
-- Additive: the constraint is REPLACED by a strictly wider one, so no row that
-- was legal yesterday becomes illegal today. Verified before writing: all 874
-- live taxonomy ids match the new pattern, and 0 rows of `master_data_nodes`
-- would be invalidated.

BEGIN;

ALTER TABLE app.master_data_nodes
    DROP CONSTRAINT IF EXISTS master_data_nodes_id_check;

-- Two bodies, because there are two minters and they disagree:
--   [0-9A-HJKMNP-TV-Z]{26}  a Crockford ULID, from every Python `_mint`;
--   [0-9a-f]{26}            the md5 slice of the organization seed trigger.
-- Stating both is the honest description. Loosening the body to `.{26}` would
-- have covered them and stopped saying anything.
ALTER TABLE app.master_data_nodes
    ADD CONSTRAINT master_data_nodes_id_check
    CHECK (id ~ '^(mdnode|bdm|bd|bcl)_([0-9A-HJKMNP-TV-Z]{26}|[0-9a-f]{26})$');

COMMENT ON COLUMN app.master_data_nodes.id IS
    'Story 49.2: a minted mdnode_ id, or a carried bdm_/bd_/bcl_ id from Story 45.1 so that every consumer pinning it keeps resolving. Two body shapes because there are two minters: a Crockford ULID from core.business_taxonomy, and a 26-character md5 slice from the organization seed trigger of migration 130.';

COMMIT;
