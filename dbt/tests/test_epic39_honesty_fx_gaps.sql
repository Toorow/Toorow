-- test_epic39_honesty_fx_gaps.sql -- Story 39.9 (AC2, E39-NFR02, honesty side).
--
-- Fail-closed at the WAREHOUSE layer for the missing-FX-pair honesty row: the fixed-tier dbt FX
-- JOIN must NOT fabricate a rate for the uncovered pair (GBP -> EUR is absent from fx_rates.csv).
-- The validation-fixture connector is NOT staging-wired (it is a namespaced seed), so a LEFT JOIN
-- onto the seed rate for GBP yields a NULL conversion -- never a 1.0-defaulted number. This
-- asserts the VALIDATION FIXTURE's honesty; it leaves the incumbent staging path (which uses
-- COALESCE(rate,1.0)) completely untouched (that is a separate migration wave, not 39.9).
--
-- The typed MissingFxPair CODE assertion lives in the offline suite; this dbt test proves the
-- warehouse side never lands a fabricated GBP total: the uncovered-pair row has NO seed rate, so
-- any convert-first attempt is NULL (honest), never 1.0.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

WITH gbp_row AS (
    SELECT
        f.project_id,
        f.date,
        f.connector,
        f.source_currency,
        CAST(f.value_decimal AS DOUBLE) AS source_amount
    FROM {{ ref('epic39_validation_fixture') }} f
    WHERE f.scenario = 'honesty_missing_fx_pair'
),
rates AS (
    SELECT from_currency, to_currency, rate
    FROM {{ ref('fx_rates') }}
    WHERE to_currency = 'EUR'
),
-- LEFT JOIN so an uncovered pair yields a NULL rate (never dropped, never 1.0-defaulted).
joined AS (
    SELECT
        g.project_id,
        g.date,
        g.connector,
        g.source_currency,
        g.source_amount,
        fx.rate AS seed_rate,
        -- the honest convert-first result: NULL when no seed pair exists (no fabricated number).
        g.source_amount * fx.rate AS converted_amount
    FROM gbp_row g
    LEFT JOIN rates fx
      ON fx.from_currency = g.source_currency
),
-- CARDINALITY GUARD: the uncovered-pair row must exist, else this proves nothing.
cardinality_guard AS (
    SELECT
        CAST(NULL AS VARCHAR) AS project_id,
        CAST(NULL AS DATE) AS date,
        CAST(NULL AS VARCHAR) AS source_currency,
        CAST(NULL AS DOUBLE) AS seed_rate,
        CAST(NULL AS DOUBLE) AS converted_amount,
        'CARDINALITY_FAIL: honesty_missing_fx_pair (GBP) row absent -- seed not run or fixture emptied'
            AS failure_reason
    WHERE (SELECT COUNT(*) FROM gbp_row) = 0
),
-- FABRICATION VIOLATION: for the uncovered pair, the seed rate MUST be NULL (no pair) and the
-- converted amount MUST therefore be NULL. A non-NULL rate/amount means a fabricated conversion
-- (e.g. a 1.0 fallback leaked in) -- the exact honesty failure this gate forbids.
fabrication AS (
    SELECT
        project_id,
        date,
        source_currency,
        seed_rate,
        converted_amount,
        'FABRICATION_FAIL: uncovered pair produced a non-NULL rate/conversion (a fallback leaked in)'
            AS failure_reason
    FROM joined
    WHERE seed_rate IS NOT NULL OR converted_amount IS NOT NULL
)
SELECT project_id, date, source_currency, seed_rate, converted_amount, failure_reason FROM cardinality_guard
UNION ALL
SELECT project_id, date, source_currency, seed_rate, converted_amount, failure_reason FROM fabrication
