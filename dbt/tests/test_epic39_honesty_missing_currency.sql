-- test_epic39_honesty_missing_currency.sql -- Story 39.9 (AC2, E39-NFR02, honesty side).
--
-- Fail-closed at the WAREHOUSE layer for the missing-currency honesty row: a monetary row whose
-- source_currency is blank/absent must NEVER land a naked money total (no EUR/project-currency
-- default is fabricated). The typed CURRENCY_GAP / UNKNOWN_CURRENCY_GAP CODE assertion lives in
-- the offline suite (test_epic39_validation.py) because it is a core detector output, not a mart
-- column; this dbt test owns the warehouse-side invariant: the blank-currency monetary row is
-- present in the fixture (cardinality guard) AND it carries NO resolvable currency, so any sum
-- that included it would be a silent wrong total. We assert the row exists and is honestly
-- un-currencied -- the read layer must exclude it from any aggregate (proven in the offline
-- refusal test), never coerce a default here.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

WITH honesty_row AS (
    SELECT
        project_id,
        date,
        connector,
        metric,
        source_currency,
        value_decimal
    FROM {{ ref('epic39_validation_fixture') }}
    WHERE scenario = 'honesty_missing_currency'
),
-- CARDINALITY GUARD: the honesty row must exist, else this proves nothing.
cardinality_guard AS (
    SELECT
        CAST(NULL AS VARCHAR) AS project_id,
        CAST(NULL AS DATE) AS date,
        CAST(NULL AS VARCHAR) AS connector,
        CAST(NULL AS VARCHAR) AS source_currency,
        'CARDINALITY_FAIL: honesty_missing_currency row absent -- seed not run or fixture emptied'
            AS failure_reason
    WHERE (SELECT COUNT(*) FROM honesty_row) = 0
),
-- FABRICATION VIOLATION: the honesty row must be honestly un-currencied. If it somehow carries a
-- non-blank currency, the fixture no longer exercises the missing-currency case (a default was
-- fabricated in the data, defeating the honesty proof).
fabrication AS (
    SELECT
        project_id,
        date,
        connector,
        source_currency,
        'FABRICATION_FAIL: honesty_missing_currency row carries a currency (a default was fabricated)'
            AS failure_reason
    FROM honesty_row
    WHERE source_currency IS NOT NULL AND source_currency <> ''
)
SELECT project_id, date, connector, source_currency, failure_reason FROM cardinality_guard
UNION ALL
SELECT project_id, date, connector, source_currency, failure_reason FROM fabrication
