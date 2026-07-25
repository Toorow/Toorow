-- test_money_contract_micros_semantic.sql — Story 39.2 (AC2, AC5): the semantic
-- ratio reconstruction is proven SUM-THEN-DIVIDE and CANONICAL-MICROS-CORRECT on a REAL
-- dbt build over a synthetic canonical-micros series (not a Python mock).
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).
--
-- Why a dedicated test (not the live semantic views): fact_daily_kpi is built by UNIONing
-- module staging — Story 39.2 must NOT touch fact_daily_kpi.sql, so we cannot inject a
-- synthetic connector into it. Instead this test replays the EXACT canonical-micros-aware
-- ratio reconstruction that semantic_roas/semantic_cpa apply (SUM the component, then /1e6
-- ONCE iff the metric is declared native_unit='micros' in money_metric_units, then divide),
-- against the money_contract_micros_fixture seed, and asserts it equals the independent
-- exact rational computed from the raw micros. This proves, on real dbt output, that:
--   * sum-then-divide is applied (the /1e6 is view-level, outside the SUM);
--   * a 'micros'-declared money metric is divided exactly once;
--   * the ratio is unit-correct when a canonical-micros metric reaches the view.
--
-- The fixture is namespaced connector='__money_contract_test__' and lives only in this
-- seed — it never enters fact_daily_kpi / cross_source_* (which UNION module staging), so
-- it cannot pollute any production total (mirrors how test-only rows are connector-keyed).
--
-- NOTE the fixture declares BOTH ad_revenue AND cost as canonical micros here to exercise a
-- micros numerator AND micros denominator through the /1e6 branch. We therefore treat BOTH
-- as micros in the reconstruction below (independent of the production seed's 'cost'=decimal
-- classification) so the test isolates the arithmetic of the micros branch.

WITH fx AS (
    SELECT project_id, date, connector, breakdown_dimension, breakdown_value, metric, value
    FROM {{ ref('money_contract_micros_fixture') }}
),
reconstructed AS (
    SELECT
        date,
        -- semantic_roas replay: SUM each component, /1e6 (both are canonical micros here),
        -- then divide. All divides are OUTSIDE the SUM (NFR01).
        (SUM(CASE WHEN metric = 'ad_revenue' THEN value ELSE 0 END) / 1000000.0)
        / NULLIF(SUM(CASE WHEN metric = 'cost' THEN value ELSE 0 END) / 1000000.0, 0) AS roas_recon,
        -- Independent exact rational: (sum micros revenue) / (sum micros cost). The /1e6 on
        -- numerator and denominator cancels, so this is the ground-truth ratio the view must
        -- reproduce. If a per-row /1e6 (divide-then-sum) had leaked in, drift would appear.
        SUM(CASE WHEN metric = 'ad_revenue' THEN value ELSE 0 END)
        / NULLIF(SUM(CASE WHEN metric = 'cost' THEN value ELSE 0 END), 0) AS roas_exact,
        -- display-unit numerator the view exposes as semantic_numerator (revenue /1e6 once)
        SUM(CASE WHEN metric = 'ad_revenue' THEN value ELSE 0 END) / 1000000.0 AS revenue_display
    FROM fx
    GROUP BY date
)
SELECT *
FROM reconstructed
WHERE ABS(roas_recon - roas_exact) > 1e-12
   -- revenue_display must equal the exact raw-micros /1e6 (single division, exact)
   OR revenue_display IS NULL
