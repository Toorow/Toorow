-- test_candidate_provenance_complete.sql -- Story 12.5 AC3.
--
-- After a PUBLICATION-TRIGGERED dbt build (with the three execution-provenance
-- dbt variables set), candidate_full_grain must carry REAL provenance -- no NULL
-- execution_id, mapping_version_id, or plan_version_id remains. This proves AD-9:
-- the three NULLs Story 12.4 carried honestly are now real values (never
-- fabricated), completing the provenance chain 12.4 deferred.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass). This test is
-- GATED on the publication context: it only asserts completeness when the build
-- was run with execution_id set (the publication path). When built WITHOUT the
-- variable (pure-dbt v1 / CI), var('execution_id') is NULL and the test is
-- vacuously satisfied -- the honest-NULL path is legitimate there and must not
-- fail CI. Run it with the variables to prove backfill:
--
--   dbt build --select candidate_full_grain \
--     --vars '{"execution_id":"dse_test","mapping_version_id":"dmap_test","plan_version_id":"dsp_test"}'
--   dbt test --select test_candidate_provenance_complete
--
-- Zero seed rows also pass (no candidate materialized yet) -- correct: the
-- completeness claim is vacuously true on an empty relation.

{% set exec_var = var('execution_id', none) %}

{% if exec_var is none %}

-- No publication context (pure-dbt v1 / CI): honest NULL is legitimate. The
-- completeness assertion does not apply; emit zero rows (pass).
SELECT 1 AS execution_id WHERE 1 = 0

{% else %}

-- Publication context: EVERY row must carry real provenance. Any row with a NULL
-- in the three backfilled columns is a FAIL (returned row = leaked honest-NULL
-- that should have been backfilled).
SELECT
    grain_key,
    execution_id,
    mapping_version_id,
    plan_version_id
FROM {{ ref('candidate_full_grain') }}
WHERE execution_id IS NULL
   OR mapping_version_id IS NULL
   OR plan_version_id IS NULL

{% endif %}
