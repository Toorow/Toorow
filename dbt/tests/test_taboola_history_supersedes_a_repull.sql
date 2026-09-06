-- Two pulls of the same Taboola history record must yield ONE staged row.
--
-- WHAT THIS CLOSES. `stg_taboola_history` was the one module staging of 54 that
-- did not supersede on `pull_id`: a bare `SELECT *` over an append-only raw
-- table (AD-7), with a `unique` test on `record_id` that a second pull of any
-- overlapping window turned red on real data. That is the execution-substrate
-- criterion "a retried unit is not idempotent" -- retry is the repair mechanism
-- of this stack, so a landing path that duplicates on retry breaks the decision
-- to put Cloud Tasks under every pull.
--
-- THE GRAIN-UNIQUE TEST IN schema.yml IS NOT THIS TEST. It says "no duplicate in
-- the staged output", which a `SELECT *` also satisfies on any fixture holding a
-- single pull. This one says the harder thing: that the raw table HOLDS a
-- re-pull, that the collision collapses, and that the SURVIVOR is the latest
-- pull rather than an arbitrary one.
--
-- AND IT REFUSES TO MEASURE ITS OWN COPY. If `raw_taboola_history` ever stops
-- carrying two pulls of a record, the first branch below would go green over a
-- restored `SELECT *`. So the second branch fails the test in exactly that case:
-- a fixture without a retry is a red test, not a silent one. The seed loader
-- (`server/modules/taboola/seeds/load_taboola_seed.py`) lands the golden records
-- twice, under two sorted pull ids, for this reason.
--
-- dbt convention: a singular test FAILS when the query returns rows.
--
-- Portability: `QUALIFY` is not used here, and every construct below (window
-- function, LEFT JOIN, string concat over the grain) compiles on DuckDB and on
-- BigQuery, which is the pair `execution-substrate` requires.

WITH landed AS (
    SELECT
        project_id,
        account_id,
        record_id,
        COUNT(DISTINCT pull_id) AS pulls,
        MAX(pull_id) AS latest_pull_id
    FROM {{ source('raw_taboola', 'raw_taboola_history') }}
    GROUP BY project_id, account_id, record_id
),

staged AS (
    SELECT
        project_id,
        account_id,
        record_id,
        COUNT(*) AS staged_rows,
        MAX(pull_id) AS staged_pull_id
    FROM {{ ref('stg_taboola_history') }}
    GROUP BY project_id, account_id, record_id
),

compared AS (
    SELECT
        landed.project_id,
        landed.account_id,
        landed.record_id,
        landed.latest_pull_id,
        staged.staged_rows,
        staged.staged_pull_id
    FROM landed
    LEFT JOIN staged
        ON staged.project_id = landed.project_id
       AND staged.account_id = landed.account_id
       AND staged.record_id = landed.record_id
),

-- A landed grain that the staging drops, duplicates, or keeps under an older
-- pull than the one that last landed it.
not_superseded AS (
    SELECT
        'a re-pulled history record is dropped, duplicated, or kept from an older pull'
            AS failure,
        project_id,
        account_id,
        record_id
    FROM compared
    WHERE staged_rows IS NULL
       OR staged_rows <> 1
       OR staged_pull_id <> latest_pull_id
),

-- The instrument checking itself: no record was ever pulled twice, so nothing
-- above could have failed.
no_repull_to_measure AS (
    SELECT
        'raw_taboola_history holds no re-pulled record: this test would pass on a bare SELECT *'
            AS failure,
        '' AS project_id,
        '' AS account_id,
        '' AS record_id
    FROM (SELECT MAX(pulls) AS max_pulls FROM landed) AS coverage
    WHERE coverage.max_pulls < 2
)

SELECT failure, project_id, account_id, record_id FROM not_superseded
UNION ALL
SELECT failure, project_id, account_id, record_id FROM no_repull_to_measure
