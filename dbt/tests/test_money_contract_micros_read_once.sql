-- test_money_contract_micros_read_once.sql — Story 39.2 (AC1, E39-AD1).
--
-- Structural regression guard: no mart/staging model divides a canonical-micros money
-- metric UPSTREAM. The /1e6 for a canonical-micros metric must happen EXACTLY ONCE, at read
-- (the semantic views + core.money.read_units) — never in fact_daily_kpi nor in staging. If a
-- future connector (e.g. GAM wired into the mart) lands a 'micros'-declared money metric,
-- fact_daily_kpi must carry the RAW canonical-micros magnitude (an integer count of micros),
-- NOT a display-unit value already divided by 1e6.
--
-- Today NO wired block emits a 'micros'-declared money metric (GAM is not in dbt_project.yml
-- model-paths and has no fact_daily_kpi block — verified), so this test returns 0 rows and
-- stays green. It becomes load-bearing the day a canonical-micros metric reaches the mart: it
-- FAILS if such a metric was pre-divided upstream (fractional value, i.e. it lost its
-- integer-micros exactness before read — the signature of a rogue upstream /1e6 or an adapter
-- bypass).
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

WITH micros_money AS (
    SELECT canonical_metric
    FROM {{ ref('money_metric_units') }}
    WHERE canonical_native_unit = 'micros'
)
SELECT
    f.project_id,
    f.date,
    f.connector,
    f.metric,
    f.value
FROM {{ ref('fact_daily_kpi') }} f
WHERE f.metric IN (SELECT canonical_metric FROM micros_money)
  -- A canonical-micros value in the mart is an integer count of micros. A non-integer value
  -- means it was divided (or otherwise rescaled) upstream — the regression this guards.
  AND f.value <> CAST(f.value AS BIGINT)
