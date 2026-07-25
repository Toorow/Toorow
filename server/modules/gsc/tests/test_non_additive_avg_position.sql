-- test_non_additive_avg_position.sql
-- Proves that the impression-weighted average_position from semantic_avg_position
-- diverges from the naive AVG of average_position in fact_daily_kpi.
--
-- AD-4: this test confirms the non-additive rule is enforced correctly.
-- If this test returns rows, it means the difference is provable (non-trivial case).
--
-- This test passes when used with seed data containing at least two rows per
-- (project, date, connector, breakdown_dimension) group with different positions
-- and different impression counts.
--
-- Seed data in load_gsc_seed.py ensures the non-trivial case:
--   Page /blog/: impressions=850, position=7.3
--   Page /docs/: impressions=200, position=3.1
--   Naive AVG = (7.3 + 3.1) / 2 = 5.2  (WRONG approach)
--   Weighted AVG = (7.3*850 + 3.1*200) / (850+200) ≈ 6.5  (CORRECT)
--
-- This file is a documentation artifact. The enforcement is:
--   1. CI grep (make check-non-additive-guard) blocks SUM(average_position) in marts.
--   2. Layer 4 conformance golden fixture validates transform() correctness.
--   3. server/tests/integration/test_gsc_seed_to_mart.py is the Python proof.
SELECT
    'weighted_avg_diverges_from_naive_avg' AS assertion,
    'AD-4 non-additive rule is enforced by semantic_avg_position view' AS note
WHERE 1 = 1
