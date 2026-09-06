-- The Story 48.3 money contract, asserted where `cross_source_revenue` DERIVES it
-- rather than where it merely holds. Singular dbt test: 0 rows = PASS.
--
-- WHAT THE COLUMN TEST DOES NOT SAY. `cross_source_revenue_total_null_iff_money_gap`
-- reads the mart alone: it catches a withheld total with no reason and a stated
-- total carrying one, and it is satisfied by a model that never withholds and
-- never explains -- which is exactly the model that existed before this change.
-- This test reads the mart AGAINST `fact_daily_kpi`, so it says the harder thing:
-- that the day's verdict follows from the rows the day is actually made of.
--
-- THE WINNING SOURCE IS THE ONLY ONE THAT COUNTS. `cross_source_revenue` picks ONE
-- connector per (project, date) by declared priority and totals it over ONE
-- canonical breakdown dimension (AD-4: summing across sources would double-count a
-- Stripe payment settling a Shopify order). So the gap belongs to the rows that
-- BUILT the stated figure. A losing source that could not convert changes nothing
-- about the winner's total, and smearing its code onto the day would tell a reader
-- that a complete figure is incomplete.
--
-- FOUR BRANCHES. The first two are the contract; the last two are the instrument
-- refusing to measure its own copy.
--
--   1. gap_not_stated -- the winning rows include one that could not convert, and
--      the day states a total, or states no reason. This is the shape a warehouse
--      with no reporting currency produces on nearly every day, and the shape the
--      replaced `not_null` used to report as a defect of the computation.
--   2. gap_invented -- every winning row converted, and the day nevertheless
--      withholds its total or carries a code. This branch is exercised by every
--      converted day in the fixture, and it is what catches a model that sets the
--      gap unconditionally or that inherits a LOSING source's code.
--   3. no_revenue_day_to_measure -- the relation is empty, so branches 1 and 2
--      compared nothing.
--   4. no_unconvertible_revenue_row_in_the_fixture -- `fact_daily_kpi` carries no
--      unconvertible revenue row AT ALL, so nothing in this warehouse could ever
--      have reached branch 1 and its silence means nothing. A fixture that stops
--      landing money the FX seed cannot convert is a RED test, not a quiet one.
--
-- Portability: no QUALIFY, no FILTER clause, no `::` cast -- every construct here
-- compiles on DuckDB and on BigQuery, the pair `execution-substrate` requires.
--
-- AND THE SENTINEL BRANCHES CARRY THE COLUMN'S OWN TYPE (2026-09-01). Branches 3
-- and 4 fill the three payload columns with a placeholder, and `date` used to get
-- `''` -- a STRING where branches 1 and 2 hand a DATE. DuckDB coerces the two in
-- a UNION and said nothing; BigQuery answers *Column 3 in UNION ALL has
-- incompatible types: DATE, DATE, STRING, STRING* and refuses to plan the test at
-- all, on ANY warehouse, with or without rows. `CAST(NULL AS DATE)` is the
-- placeholder both engines accept, and it is the more honest one: a branch that
-- names no day should say NO day, not the empty string.

WITH rev AS (
    SELECT project_id, date, connector, breakdown_dimension, value, money_gap_code
    FROM {{ ref('fact_daily_kpi') }}
    WHERE metric = 'revenue'
),

-- The model's own two steps, re-derived from the priority seed rather than read
-- back from the model: the winning connector, then the one canonical dimension.
winners AS (
    SELECT project_id, date, connector
    FROM (
        SELECT
            c.project_id,
            c.date,
            c.connector,
            ROW_NUMBER() OVER (
                PARTITION BY c.project_id, c.date
                ORDER BY COALESCE(p.priority, 99), c.connector
            ) AS rnk
        FROM (SELECT DISTINCT project_id, date, connector FROM rev) c
        LEFT JOIN {{ ref('metric_source_priority') }} p
            ON p.metric = 'revenue' AND p.connector = c.connector
    ) ranked
    WHERE rnk = 1
),

canonical_dim AS (
    SELECT project_id, date, connector, MIN(breakdown_dimension) AS dim
    FROM rev
    GROUP BY project_id, date, connector
),

-- What the day is MADE OF: the rows that contribute to the stated figure.
contributing AS (
    SELECT
        f.project_id,
        f.date,
        SUM(CASE WHEN f.value IS NULL THEN 1 ELSE 0 END) AS unconvertible_rows,
        COUNT(*) AS contributing_rows
    FROM rev f
    JOIN winners w
        ON w.project_id = f.project_id AND w.date = f.date AND w.connector = f.connector
    JOIN canonical_dim d
        ON d.project_id = f.project_id AND d.date = f.date
       AND d.connector = f.connector AND d.dim = f.breakdown_dimension
    GROUP BY f.project_id, f.date
),

stated AS (
    SELECT project_id, date, revenue_total, money_gap_code, revenue_source
    FROM {{ ref('cross_source_revenue') }}
),

judged AS (
    SELECT
        s.project_id,
        s.date,
        s.revenue_source,
        s.revenue_total,
        s.money_gap_code,
        c.unconvertible_rows,
        c.contributing_rows
    FROM stated s
    LEFT JOIN contributing c
        ON c.project_id = s.project_id AND c.date = s.date
),

-- 1. A day built from an unconvertible row that still states a figure, or states
--    no reason for withholding it.
gap_not_stated AS (
    SELECT
        'a day whose winning source could not convert states a total, or states no reason'
            AS failure,
        project_id,
        date,
        revenue_source
    FROM judged
    WHERE unconvertible_rows > 0
      AND (revenue_total IS NOT NULL OR money_gap_code IS NULL)
),

-- 2. A day every one of whose rows converted, yet withholds its total or carries
--    a gap code -- including a code inherited from a LOSING source.
gap_invented AS (
    SELECT
        'a day whose winning source converted withholds its total, or carries a gap code'
            AS failure,
        project_id,
        date,
        revenue_source
    FROM judged
    WHERE unconvertible_rows = 0
      AND (revenue_total IS NULL OR money_gap_code IS NOT NULL)
),

-- 3. The relation is empty: the two branches above compared nothing.
no_day_to_measure AS (
    SELECT
        'cross_source_revenue holds no row: this test compared nothing' AS failure,
        '' AS project_id,
        CAST(NULL AS DATE) AS date,
        '' AS revenue_source
    FROM (SELECT COUNT(*) AS days FROM stated) AS coverage
    WHERE coverage.days = 0
),

-- 4. The instrument checking itself: no revenue row in this warehouse could fail
--    to convert, so branch 1 is structurally unable to fire and its silence
--    proves nothing about the gap column.
no_unconvertible_row_to_measure AS (
    SELECT
        'fact_daily_kpi holds no unconvertible revenue row: the gap branch is unreachable here'
            AS failure,
        '' AS project_id,
        CAST(NULL AS DATE) AS date,
        '' AS revenue_source
    FROM (
        SELECT SUM(CASE WHEN value IS NULL THEN 1 ELSE 0 END) AS gapped FROM rev
    ) AS coverage
    WHERE COALESCE(coverage.gapped, 0) = 0
)

SELECT failure, project_id, date, revenue_source FROM gap_not_stated
UNION ALL
SELECT failure, project_id, date, revenue_source FROM gap_invented
UNION ALL
SELECT failure, project_id, date, revenue_source FROM no_day_to_measure
UNION ALL
SELECT failure, project_id, date, revenue_source FROM no_unconvertible_row_to_measure
