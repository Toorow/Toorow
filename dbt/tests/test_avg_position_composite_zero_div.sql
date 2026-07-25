-- AI-52 (Epic 8): live guard on the deployed semantic_avg_position_composite VIEW.
-- Two invariants over the REAL GSC data (complements the inline-fixture arithmetic
-- test test_avg_position_composite_weighting.sql):
--   (1) Zero-division safety: whenever impressions_weight = 0 the weighted
--       average_position MUST be NULL (the NULLIF guard held -- no divide error, no
--       fabricated number). A row with weight 0 but a non-NULL position is a bug.
--   (2) Encoding: breakdown_dimension is a KNOWN composite dimension and the
--       breakdown_value is the '>'-joined parent>child path (contains exactly one '>').
--       Story 10.5 added the 'query>page' composite alongside AI-52's 'country>device';
--       both are impression-weighted composites in this view and share the same
--       zero-div + single-'>' encoding invariants.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

SELECT
    project_id,
    date,
    breakdown_dimension,
    breakdown_value,
    impressions_weight,
    average_position
FROM {{ ref('semantic_avg_position_composite') }}
WHERE
    -- (1) zero weight must yield NULL position
    (COALESCE(impressions_weight, 0) = 0 AND average_position IS NOT NULL)
    -- (2) dimension/value encoding must be a known composite path form
    OR breakdown_dimension NOT IN ('country>device', 'query>page')
    OR breakdown_value NOT LIKE '%>%'
    OR breakdown_value LIKE '%>%>%'
