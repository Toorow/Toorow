-- test_fx_rate_date_is_the_quotation_date.sql -- story 58.7, arbitrage 3.
--
-- THE DATE UNDER AN AMOUNT HAS TO BE THE DAY THE RATE WAS QUOTED.
--
-- A rate of `dbt/seeds/fx_rates.csv` carries two dates:
--
--   from_currency,to_currency,rate,rate_date,rate_policy,valid_from,valid_to
--   USD,EUR,0.92,2026-07-01,static_dev_rate,2020-01-01,2099-12-31
--
-- `valid_from` is where the validity WINDOW opens; `rate_date` is when the rate was
-- QUOTED. Eight staging models published the first under the name of the second, so
-- `fx_as_of_date` was `2020-01-01` on 43 of the 43 converted rows of the mart --
-- measured 2026-08-07, without exception. Story 58.7 was about to print that under
-- every amount on screen: a provenance that dates nothing, which is exactly the
-- invented evidence the story exists to prevent. The right column was in the same
-- seed, unread.
--
-- IT REPLAYS OVER A VERSIONED SEED, and that is deliberate. `fact_daily_kpi` is
-- built from module STAGING over raw relations, and a fresh checkout has landed no
-- raw row at all: an assertion written only over the mart would be a zero-row pass
-- for everyone who had not run somebody's script -- the defect story 58.5 was
-- rejected for. `dbt/seeds/fx_rate_date_fixture.csv` is loaded by `dbt seed` in
-- every environment, so the evidence travels with the repository.
--
-- WHAT THE REPLAY CAN AND CANNOT PROVE. It proves that the two columns are two
-- different facts, that one of them is constant across quotations and the other is
-- not, and that the validity window -- not the quotation date -- is what SELECTS a
-- rate. It does NOT prove that the eight staging models read the right one: a seed
-- cannot enter `fact_daily_kpi`. That is
-- `server/tests/core/test_money_provenance_columns.py`, which reads the eight
-- models' own text and reddens by name the moment `fx.valid_from` comes back --
-- on a fresh checkout, with no database at all. The call site below is exact on a
-- warehouse that carries converted rows and vacuous on one that does not, which is
-- the same shape `test_country_bucket_absence.sql` ends with.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass). Six
-- assertions, and the first two exist so the other four cannot pass by having
-- nothing to look at.

WITH fixture AS (
    SELECT
        from_currency,
        to_currency,
        CAST(rate AS {{ toorow_float_type() }})       AS rate,
        CAST(rate_date AS DATE)    AS rate_date,
        CAST(valid_from AS DATE)   AS valid_from,
        CAST(valid_to AS DATE)     AS valid_to,
        scenario
    FROM {{ ref('fx_rate_date_fixture') }}
),

-- (a) ANTI-VACUITY. No `open_window` row means the fixture was emptied or the seed
-- did not run, and every check below would be a zero-row pass.
cardinality_guard AS (
    SELECT
        CAST(NULL AS {{ toorow_string_type() }})                            AS pair,
        CAST(NULL AS DATE)                               AS quoted_on,
        CAST(NULL AS DATE)                               AS window_opened_on,
        'CARDINALITY_FAIL: no `open_window` row in fx_rate_date_fixture -- the '
        || 'seed did not run or the fixture was emptied'  AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM fixture WHERE scenario = 'open_window') = 0
),

-- (b) AND THE FIXTURE STILL SEPARATES THE TWO FACTS. Align `rate_date` with
-- `valid_from` and this file stops proving anything at all, silently -- so it says
-- so instead.
fixture_stopped_separating AS (
    SELECT
        CAST(NULL AS {{ toorow_string_type() }})                            AS pair,
        CAST(NULL AS DATE)                               AS quoted_on,
        CAST(NULL AS DATE)                               AS window_opened_on,
        'FIXTURE_NO_LONGER_SEPARATES: every row now quotes on the day its window '
        || 'opened, so this file can no longer tell the two columns apart'
                                                         AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM fixture WHERE rate_date <> valid_from) = 0
),

-- (c) ONE OF THE TWO DATES IS A MEASUREMENT AND THE OTHER IS NOT. Across the rows
-- that share a validity window, `valid_from` takes ONE value and `rate_date` takes
-- several. That is the defect in one line: a column that never varies cannot be a
-- quotation date, and 43 rows of the mart carried it as one.
open_window_dates AS (
    SELECT
        COUNT(DISTINCT valid_from) AS distinct_window_starts,
        COUNT(DISTINCT rate_date)  AS distinct_quotations
    FROM fixture
    WHERE scenario = 'open_window'
),

window_start_is_not_a_quotation AS (
    SELECT
        'open_window'                                    AS pair,
        CAST(NULL AS DATE)                               AS quoted_on,
        CAST(NULL AS DATE)                               AS window_opened_on,
        'WINDOW_START_MISTAKEN_FOR_A_QUOTATION: these rows no longer show that '
        || 'valid_from is constant while rate_date varies -- the one measurement '
        || 'this fixture exists to hold'                 AS failure_reason
    FROM open_window_dates
    WHERE distinct_window_starts <> 1
       OR distinct_quotations < 2
),

-- (d) THE WINDOW IS STILL WHAT CHOOSES. The staging join is
-- `CAST(raw.date AS DATE) BETWEEN fx.valid_from AND fx.valid_to`, replayed here on
-- a probe date. Exactly one bounded row may answer it: confusing the two roles --
-- selecting on `rate_date` -- would change WHICH rate a row is converted at, which
-- is a far worse defect than the one being repaired.
selected AS (
    SELECT COUNT(*) AS matched
    FROM fixture
    WHERE scenario = 'bounded_window'
      AND from_currency = 'JPY'
      AND to_currency = 'EUR'
      AND DATE '2026-07-10' BETWEEN valid_from AND valid_to
),

selection_broken AS (
    SELECT
        'JPY>EUR'                                        AS pair,
        CAST(NULL AS DATE)                               AS quoted_on,
        CAST(NULL AS DATE)                               AS window_opened_on,
        'RATE_SELECTION_BROKEN: the validity window no longer selects exactly one '
        || 'rate for a date inside it -- valid_from/valid_to CHOOSE, rate_date '
        || 'only DATES'                                  AS failure_reason
    FROM selected
    WHERE matched <> 1
),

-- (e) AND THE SELECTED ROW IS NOT DATED BY ITS WINDOW. The July rate was quoted two
-- days after its window opened; printing the window's start would have said
-- `2026-07-01` for a rate quoted on the 3rd.
selected_row AS (
    SELECT *
    FROM fixture
    WHERE scenario = 'bounded_window'
      AND from_currency = 'JPY'
      AND to_currency = 'EUR'
      AND DATE '2026-07-10' BETWEEN valid_from AND valid_to
),

selected_row_dated_by_its_window AS (
    SELECT
        from_currency || '>' || to_currency              AS pair,
        rate_date                                        AS quoted_on,
        valid_from                                       AS window_opened_on,
        'SELECTED_ROW_DATED_BY_ITS_WINDOW: the row chosen by the window is quoted '
        || 'on the day the window opened, so this case can no longer show the '
        || 'difference'                                  AS failure_reason
    FROM selected_row
    WHERE rate_date = valid_from
),

-- (f) THE CALL SITE. Exact on a warehouse that carries converted rows, vacuous on
-- one that does not -- and it is kept rather than dropped, because it is the only
-- statement here that looks at what the models really published. `2020-01-01` is
-- the value the eight models emitted before this story; the seed's own windows all
-- open then, and no rate of this product was ever quoted in 2020.
mart_provenance AS (
    SELECT
        connector || '>' || metric                       AS pair,
        fx_as_of_date                                    AS quoted_on,
        DATE '2020-01-01'                                AS window_opened_on,
        'MART_DATES_ITS_RATE_BY_A_WINDOW: fact_daily_kpi carries a quotation date '
        || 'no rate of this repository was ever quoted on -- a staging model is '
        || 'publishing fx.valid_from under the name of fx.rate_date'
                                                         AS failure_reason
    FROM {{ ref('fact_daily_kpi') }}
    WHERE fx_rate IS NOT NULL
      AND fx_as_of_date IS NOT NULL
      AND fx_as_of_date < DATE '2021-01-01'
)

SELECT * FROM cardinality_guard
UNION ALL
SELECT * FROM fixture_stopped_separating
UNION ALL
SELECT * FROM window_start_is_not_a_quotation
UNION ALL
SELECT * FROM selection_broken
UNION ALL
SELECT * FROM selected_row_dated_by_its_window
UNION ALL
SELECT * FROM mart_provenance
