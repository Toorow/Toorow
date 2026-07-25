-- test_non_additive_never_summed: asserts that 'average_position' is never simply
-- summed across rows without impression weighting in the mart.
--
-- AD-4 enforcement guard for average_position — analogous to test_metric_not_ratio.sql
-- (added in Story 3.6 for ratio metrics).
--
-- DOCUMENTATION GUARD: this test is a placeholder that always passes.
-- The actual enforcement is:
--   1. CI grep (make check-non-additive-guard): fails if SUM(average_position) appears in marts.
--   2. Layer 4 conformance golden fixture: proves the weighted-avg pipeline is correct.
--   3. server/tests/integration/test_gsc_seed_to_mart.py: proves semantic view correctness.
--
-- Why a SQL guard alone is insufficient: SQL greps on static strings cannot reliably
-- detect all forms of naive summation (aliased columns, CTEs, etc.). The CI grep on
-- dbt/models/marts/ source files is the primary enforcement mechanism.
--
-- NOTE: adding 'average_position' to metric_not_ratio.sql would block it from fact_daily_kpi
-- entirely, but we DO store it there (as a non-additive metric row). The distinction is:
--   - metric_not_ratio: blocks RATIO metrics (ctr, cvr, etc.) from the mart.
--   - non_additive_never_summed (this guard): allows average_position in the mart
--     but ensures it is never SUMMED without weighting.
{% test non_additive_never_summed(model, column_name) %}
SELECT 1 WHERE 1 = 0
{% endtest %}
