-- A version number designates ONE content, ever -- the authority numbers from
-- the legacy maximum.
--
-- THE DEFECT, MEASURED 2026-08-30. `master_data.create_node_version` seeded its
-- counter from the authority's own ledger alone, so a converged identity
-- restarted at 1. `core.business_identity_catalogue` unions the authority with
-- the SUPERSEDED ledger per `(id, version_number)` -- that union is what keeps a
-- convergence from losing a revision -- and resolved the overlap by letting the
-- authority win. One NUMBER could therefore designate two different CONTENTS: a
-- domain carrying legacy revisions 1..3, converged and then revised, minted
-- authority v2 while the legacy v2 held something else, and the reader silently
-- showed one of them. `governance.md` recorded it as *Named, not resolved*.
--
-- THE DECISION (Jean, 2026-08-31, AI-324): renumber. The writer now mints above
-- EVERY ledger that numbers the identity (`master_data._NEXT_NODE_VERSION_NUMBER`),
-- so no new collision can be created. This migration clears the ones that already
-- exist, which is what lets the resolver's overlap clause become an ASSERTION
-- path -- it shouts now, and it must have nothing to shout about on a healthy
-- base.
--
-- WHICH SIDE MOVES, AND WHY IT IS NOT A CHOICE. The legacy version ledgers are
-- IMMUTABLE: migration 130's `reject_business_taxonomy_version_mutation` refuses
-- UPDATE and DELETE on them, and pins in `app.golden_question_versions`,
-- `app.feedback_regression_cases` and their siblings carry FOREIGN KEYS to
-- `app.mdm_business_domain_versions (domain_id, version_number)`. So the legacy
-- numbers are load-bearing and frozen; the authority's are referenced by nothing
-- but themselves. Every foreign reference to a node version is BY `id`
-- (migrations 140, 143, 147, 311 -- all `REFERENCES … (id)`), and `id` is minted
-- once and never renumbered, so the rename lock of 2026-08-30
-- (`master_data.current_node_version_id`, `expected_version`) reads exactly what
-- it read before this ran.
--
-- WHAT MOVES: the authority revisions of an identity whose LOWEST number is at
-- or below the superseded ledger's HIGHEST. They shift as a block, keeping their
-- order and their gaps, so that the lowest lands one above the legacy maximum.
-- An identity the legacy ledger never numbered is not touched at all: its
-- `authority_min` has nothing to be compared with and the join drops it.

BEGIN;

-- The shift is applied in TWO PASSES through a parking range, because
-- `uq_master_data_versions_number_node` is a unique INDEX (not a deferrable
-- constraint) and is therefore checked row by row. Shifting v1,v2 to v2,v3 in
-- one statement can present v1 -> v2 while the old v2 is still there. The
-- parking range is disjoint from every number in use, which this asserts rather
-- than assumes.
DO $$
DECLARE
    highest INTEGER;
BEGIN
    SELECT COALESCE(MAX(version_number), 0) INTO highest
      FROM app.master_data_object_versions;
    IF highest >= 1000000 THEN
        RAISE EXCEPTION
            'version numbers reach %, which overlaps the parking range this '
            'migration shifts through. Raise the parking offset in a NEW '
            'migration before renumbering.', highest
            USING ERRCODE = '22003';
    END IF;
END $$;

-- THE GUARD IS LIFTED FOR THIS STATEMENT AND PUT BACK IN THE SAME TRANSACTION.
-- `app.protect_master_data_object_version` (migration 140) refuses any change to
-- `version_number` on a version that has left draft, and that refusal is right:
-- in normal operation a published revision's number is its name. A renumbering
-- is the one act that has to move it, and it is performed HERE -- once, by a
-- migration, on rows whose CONTENT is untouched -- rather than by weakening the
-- trigger for everyone. `DISABLE TRIGGER` takes ACCESS EXCLUSIVE, so no other
-- session can write the table while it is off, and a rollback restores it with
-- the rest of the transaction.
ALTER TABLE app.master_data_object_versions
    DISABLE TRIGGER trg_master_data_versions_protect;

CREATE TEMPORARY TABLE ai324_renumber ON COMMIT DROP AS
WITH legacy_max AS (
    SELECT domain_id AS node_id, org_id, MAX(version_number) AS legacy_max
      FROM app.mdm_business_domain_versions
     GROUP BY 1, 2
    UNION ALL
    SELECT classification_id AS node_id, org_id, MAX(version_number) AS legacy_max
      FROM app.mdm_business_classification_versions
     GROUP BY 1, 2
),
-- One row per identity even if an id somehow reached both ledgers: an UPDATE …
-- FROM that matched twice would apply an arbitrary one of the two shifts.
legacy_ceiling AS (
    SELECT node_id, org_id, MAX(legacy_max) AS legacy_max
      FROM legacy_max
     GROUP BY 1, 2
),
authority_floor AS (
    SELECT node_id, org_id, MIN(version_number) AS authority_min
      FROM app.master_data_object_versions
     WHERE node_id IS NOT NULL
     GROUP BY 1, 2
)
SELECT floor_row.node_id,
       floor_row.org_id,
       (ceiling_row.legacy_max - floor_row.authority_min + 1) AS shift
  FROM authority_floor floor_row
  JOIN legacy_ceiling ceiling_row
    ON ceiling_row.node_id = floor_row.node_id
   AND ceiling_row.org_id = floor_row.org_id
 WHERE floor_row.authority_min <= ceiling_row.legacy_max;

UPDATE app.master_data_object_versions AS version
   SET version_number = version.version_number + renumber.shift + 1000000
  FROM ai324_renumber AS renumber
 WHERE version.node_id = renumber.node_id
   AND version.org_id = renumber.org_id;

UPDATE app.master_data_object_versions
   SET version_number = version_number - 1000000
 WHERE version_number > 1000000;

ALTER TABLE app.master_data_object_versions
    ENABLE TRIGGER trg_master_data_versions_protect;

-- The state this migration exists to reach, asserted rather than hoped for: no
-- authority revision sits at a number the superseded ledger already spent for
-- the same identity.
DO $$
DECLARE
    still_colliding INTEGER;
BEGIN
    SELECT count(*) INTO still_colliding
      FROM app.master_data_object_versions version
     WHERE version.node_id IS NOT NULL
       AND (EXISTS (SELECT 1 FROM app.mdm_business_domain_versions legacy
                     WHERE legacy.domain_id = version.node_id
                       AND legacy.org_id = version.org_id
                       AND legacy.version_number = version.version_number)
         OR EXISTS (SELECT 1 FROM app.mdm_business_classification_versions legacy
                     WHERE legacy.classification_id = version.node_id
                       AND legacy.org_id = version.org_id
                       AND legacy.version_number = version.version_number));
    IF still_colliding > 0 THEN
        RAISE EXCEPTION
            '% authority revisions still share a version number with the '
            'superseded ledger after renumbering', still_colliding
            USING ERRCODE = '23505';
    END IF;

    -- And the guard is back on. A later edit that drops the re-enable would
    -- leave published version numbers writable forever, silently.
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgrelid = 'app.master_data_object_versions'::regclass
           AND tgname = 'trg_master_data_versions_protect'
           AND tgenabled = 'O'
    ) THEN
        RAISE EXCEPTION
            'trg_master_data_versions_protect was left disabled by the '
            'renumbering'
            USING ERRCODE = '23000';
    END IF;
END $$;

COMMIT;
