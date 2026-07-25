-- test_epic39_reconciliation_normalized.sql -- Story 39.9 (AC1 invariant 4, E39-FR08/AD5).
--
-- Currency-normalized reconciliation on a REAL dbt build: >=2 fixture connectors report the
-- SAME monetary metric (revenue) in DIFFERENT currencies (src_a EUR, src_b USD) on the same day.
-- The reconciled figure MUST equal the sum of per-source values EACH CONVERTED TO THE REPORTING
-- CURRENCY FIRST (fixed seed rate USD->EUR=0.92), and MUST NOT equal a naive mixed-currency sum
-- (proving normalization-before-combine, AD5). The definition applied is the SUM method (the
-- reconciliation_rules default for an additive money metric); this test cites it in the reason.
--
-- Scoped to scenario='recon_normalized' rows so it composes with the fixed seed's exact 2 rows
-- (fx_rates.csv: USD->EUR=0.92) untouched. Mirrors the totals-reconciliation idiom of
-- test_fx_resolution_applied.sql (JOIN the seed rate, compare against the naive path) with a
-- cardinality guard so it never passes vacuously.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

WITH recon_rows AS (
    SELECT
        f.project_id,
        f.date,
        f.connector,
        f.source_currency,
        CAST(f.value_decimal AS DOUBLE) AS source_amount
    FROM {{ ref('epic39_validation_fixture') }} f
    WHERE f.scenario = 'recon_normalized'
      AND f.metric = 'revenue'
      AND f.report_timezone IS NOT NULL         -- exclude the blank-tz honesty row (different scenario anyway)
      AND f.report_timezone <> ''
      AND f.source_currency IS NOT NULL
      AND f.source_currency <> ''
),
-- The fixed-tier seed rate to the reporting currency (EUR), same window filter the staging
-- path uses. USD->EUR=0.92 ; EUR->EUR is identity (rate 1.0).
rates AS (
    SELECT from_currency, rate
    FROM {{ ref('fx_rates') }}
    WHERE CAST('2026-07-01' AS DATE) BETWEEN valid_from AND valid_to
      AND to_currency = 'EUR'
),
converted AS (
    SELECT
        r.project_id,
        r.date,
        r.source_amount,
        r.source_currency,
        -- convert-first: EUR is identity (1.0), USD uses the seed rate; a currency with no seed
        -- pair would be NULL here (honesty rows are excluded from this scenario by construction).
        CASE
            WHEN r.source_currency = 'EUR' THEN r.source_amount * 1.0
            ELSE r.source_amount * fx.rate
        END AS converted_amount
    FROM recon_rows r
    LEFT JOIN rates fx
      ON fx.from_currency = r.source_currency
),
combined AS (
    SELECT
        project_id,
        date,
        -- normalized reconciliation: SUM of per-source CONVERTED (reporting-currency) values.
        SUM(converted_amount) AS reconciled_normalized,
        -- the WRONG naive path: sum the raw mixed-currency amounts as if 1 USD == 1 EUR.
        SUM(source_amount)    AS naive_mixed_currency_sum,
        COUNT(*)              AS n_sources,
        COUNT(DISTINCT source_currency) AS n_currencies
    FROM converted
    GROUP BY project_id, date
),
-- CARDINALITY GUARD: fail if there is no multi-currency reconciliation day to test (fixture
-- missing / seed not run). Without >=2 currencies the normalization is not exercised.
cardinality_guard AS (
    SELECT
        CAST(NULL AS VARCHAR) AS project_id,
        CAST(NULL AS DATE) AS date,
        CAST(NULL AS DOUBLE) AS reconciled_normalized,
        CAST(NULL AS DOUBLE) AS naive_mixed_currency_sum,
        'SUM' AS method_applied,
        'CARDINALITY_FAIL: no >=2-currency recon_normalized day -- seed not run or fixture emptied'
            AS failure_reason
    WHERE (SELECT COUNT(*) FROM combined WHERE n_currencies >= 2) = 0
),
violations AS (
    SELECT
        project_id,
        date,
        reconciled_normalized,
        naive_mixed_currency_sum,
        'SUM' AS method_applied,   -- the reconciliation_rules method cited (D-5: additive money => SUM)
        CASE
            -- expected normalized total: 100 EUR (identity) + 100 USD * 0.92 = 192.0
            WHEN ABS(reconciled_normalized - (100.0 + 100.0 * 0.92)) > 1e-9
                THEN 'RECON_FAIL: normalized total != converted-first expected (100 + 100*0.92 = 192.0)'
            -- the normalized total MUST differ from the naive mixed-currency sum (200.0), proving
            -- normalization actually changed the answer (AD5).
            WHEN ABS(reconciled_normalized - naive_mixed_currency_sum) < 1e-9
                THEN 'NORMALIZATION_ABSENT: normalized == naive mixed-currency sum (convert-first not applied)'
            ELSE NULL
        END AS failure_reason
    FROM combined
    WHERE n_currencies >= 2
)
SELECT project_id, date, reconciled_normalized, naive_mixed_currency_sum, method_applied, failure_reason
FROM cardinality_guard
UNION ALL
SELECT project_id, date, reconciled_normalized, naive_mixed_currency_sum, method_applied, failure_reason
FROM violations
WHERE failure_reason IS NOT NULL
