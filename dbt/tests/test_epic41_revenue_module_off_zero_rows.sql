-- Story 41.5 AC8 (E41-NFR01) -- MODULE OFF => ZERO ROWS, non-vacuously.
--
-- `feetaxrev_dev_off` carries REAL REVENUE FACTS with the flag FALSE. That is the whole
-- point: with no fact row an anti-join is STRUCTURALLY UNABLE TO FAIL and proves only
-- that the test ran (41.3's review finding F5). Because the facts are there, this
-- assertion can genuinely be falsified by a model that forgot to read the flag.
--
--   A. no normalized row for the OFF project;
--   B. no alignment row for it either;
--   C. THE FALSIFIER: the OFF project's revenue facts must actually EXIST in
--      fact_daily_kpi. If they do not, A and B are vacuous and that is a failure of the
--      fixture, not a pass of the model.

WITH off_normalized AS (
    SELECT 'OFF_PROJECT_HAS_A_NORMALIZED_ROW' AS failure, project_id,
           connector || '/' || metric AS detail
    FROM {{ ref('fee_tax_revenue_normalized_daily') }}
    WHERE project_id = 'feetaxrev_dev_off'
),

off_alignment AS (
    SELECT 'OFF_PROJECT_HAS_AN_ALIGNMENT_ROW' AS failure, project_id,
           tax_basis AS detail
    FROM {{ ref('fee_tax_revenue_alignment_daily') }}
    WHERE project_id = 'feetaxrev_dev_off'
),

falsifier AS (
    SELECT 'OFF_PROJECT_HAS_NO_FACTS_SO_THE_TEST_IS_VACUOUS' AS failure,
           'feetaxrev_dev_off' AS project_id,
           'seed revenue for the OFF project or this assertion cannot fail' AS detail
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE NOT EXISTS (
        SELECT 1 FROM {{ ref('fact_daily_kpi') }}
        WHERE project_id = 'feetaxrev_dev_off' AND metric = 'revenue'
    )
)

SELECT * FROM off_normalized
UNION ALL SELECT * FROM off_alignment
UNION ALL SELECT * FROM falsifier
