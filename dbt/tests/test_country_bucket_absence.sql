-- test_country_bucket_absence.sql -- story 58.5, arbitrage 1.
--
-- THE ROW WITH NO COUNTRY IS A ROW OF THE MART, NOT A ROW THAT WAS DELETED.
--
-- Two opposite behaviours used to answer the same fact, and both were latent
-- (measured 2026-08-07: zero staging rows with no country on either side, so
-- neither had ever fired). `fact_daily_kpi` composed `country || '>' ||
-- device_category` and a NULL concatenation took the nightly build down on
-- `not_null`; `int_country_daily_kpi` filtered the row out and the total by country
-- silently stopped equalling the total of the day. The second is the one nobody
-- would have learnt about.
--
-- IT REPLAYS THE MACRO OVER A VERSIONED SEED, and that is deliberate. The first
-- version of this file asserted over `fact_daily_kpi` and depended on rows landed by
-- a script somebody had to remember to run: on a fresh checkout the mart holds no
-- such row, the cardinality guard fires, and the test is RED for everyone who did
-- not know about the script. `dbt/seeds/country_absence_fixture.csv` is loaded by
-- `dbt seed` in every environment, so the evidence travels with the repository --
-- the pattern `test_epic39_*.sql` already holds over
-- `epic39_validation_fixture.csv`, and for the same reason.
--
-- WHAT THE REPLAY CAN AND CANNOT PROVE. It proves the ONE declared expression both
-- models call: the sentinel fires exactly when the source rendered nothing, the row
-- is never dropped and never NULL, and the bucketed total equals the ungrouped
-- total. It does NOT re-prove the call sites -- a seed cannot enter
-- `fact_daily_kpi`, which unions module staging and never seeds (the isolation
-- discipline of `epic39_validation_fixture` and `money_contract_micros_fixture`).
-- The call sites are held by the two model assertions at the end of this file,
-- which bite on any database that carries country rows, and by
-- `test_composite_reconciliation`.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass). Six assertions,
-- and the first exists so the other five cannot pass by having nothing to look at.

WITH replayed AS (
    SELECT
        project_id,
        connector,
        metric,
        scenario,
        CAST(value AS {{ toorow_float_type() }})                                          AS value,
        country,
        country_source,
        -- The two forms, exactly as the models call them.
        {{ country_bucket('country', 'country_source') }}               AS fail_closed_bucket,
        {{ country_bucket('country', 'country_source', retain_source_value=true) }}
                                                                       AS observable_bucket
    FROM {{ ref('country_absence_fixture') }}
),

-- (a) ANTI-VACUITY. No `absent` row means the fixture was emptied or the seed did
-- not run, and every check below would be a zero-row pass.
cardinality_guard AS (
    SELECT
        CAST(NULL AS {{ toorow_string_type() }})                            AS connector,
        CAST(NULL AS {{ toorow_string_type() }})                            AS metric,
        CAST(NULL AS {{ toorow_string_type() }})                            AS bucket,
        CAST(NULL AS {{ toorow_float_type() }})                             AS measured_total,
        CAST(NULL AS {{ toorow_float_type() }})                             AS expected_total,
        'CARDINALITY_FAIL: no `absent` row in country_absence_fixture -- '
        || 'the seed did not run or the fixture was emptied'
                                                         AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM replayed WHERE scenario = 'absent') = 0
),

-- (b) THE ABSENT ROW IS KEPT, UNDER THE SENTINEL, ON BOTH FORMS. This is the whole
-- repair: neither a NULL (which the not_null test would break on) nor a row that
-- disappeared (which nothing would report at all).
absent_not_bucketed AS (
    SELECT
        connector,
        metric,
        COALESCE(fail_closed_bucket, observable_bucket, '<NULL>')       AS bucket,
        CAST(NULL AS {{ toorow_float_type() }})                             AS measured_total,
        CAST(NULL AS {{ toorow_float_type() }})                             AS expected_total,
        'ABSENT_ROW_NOT_BUCKETED: a row with no country signal did not answer the '
        || 'declared sentinel -- it is either NULL or dropped'
                                                         AS failure_reason
    FROM replayed
    WHERE scenario = 'absent'
      AND (
          fail_closed_bucket IS DISTINCT FROM {{ country_absent_sentinel() }}
          OR observable_bucket IS DISTINCT FROM {{ country_absent_sentinel() }}
      )
),

-- (c) AND THE VOCABULARY GUARD IS STILL CLOSED. An UNRESOLVED value must stay NULL
-- under the fail-closed form -- that NULL is what makes `normalize_dimension`'s
-- not_null break the build on a spelling nobody taught the vocabulary. Folding it
-- into the sentinel would silence that guard, which is the opposite repair; the
-- observable form keeps the raw spelling instead, for governed DQ.
unresolved_swallowed AS (
    SELECT
        connector,
        metric,
        COALESCE(fail_closed_bucket, '<NULL>')           AS bucket,
        CAST(NULL AS {{ toorow_float_type() }})                             AS measured_total,
        CAST(NULL AS {{ toorow_float_type() }})                             AS expected_total,
        'UNRESOLVED_SWALLOWED: an unreadable provider value was bucketed as an '
        || 'absence, or lost its raw spelling -- two different repairs, two desks'
                                                         AS failure_reason
    FROM replayed
    WHERE scenario = 'unresolved'
      AND (
          fail_closed_bucket IS NOT NULL
          OR observable_bucket IS DISTINCT FROM country_source
      )
),

-- (d) NOTHING IS LOST. Per (connector, metric), the total over the bucketed rows
-- must equal the total over ALL rows. Restore a `WHERE bucket IS NOT NULL` anywhere
-- on this path and this is what reddens, with the exact amount that vanished.
bucketed_totals AS (
    SELECT
        connector,
        metric,
        SUM(CASE WHEN observable_bucket IS NOT NULL THEN value END)    AS measured_total,
        SUM(value)                                                     AS expected_total
    FROM replayed
    GROUP BY connector, metric
),

lost_rows AS (
    SELECT
        connector,
        metric,
        CAST(NULL AS {{ toorow_string_type() }})                            AS bucket,
        measured_total,
        expected_total,
        'COUNTRY_TOTAL_LOST: the bucketed total does not equal the ungrouped total '
        || '-- a row with no country signal was dropped'  AS failure_reason
    FROM bucketed_totals
    WHERE measured_total IS NULL
       OR ABS(measured_total - expected_total) > 0.001
),

-- (e) THE BUCKET IS NOT A PLACE. Two letters would read as a country code, and the
-- vocabulary refuses anything that is not two capitals -- so this shape check is the
-- same statement the Python reader makes in `test_geographic_semantics`.
bucket_looks_like_a_country AS (
    SELECT
        connector,
        metric,
        observable_bucket                                AS bucket,
        CAST(NULL AS {{ toorow_float_type() }})                             AS measured_total,
        CAST(NULL AS {{ toorow_float_type() }})                             AS expected_total,
        'BUCKET_IS_A_CODE: the absence bucket is shaped like a country code'
                                                         AS failure_reason
    FROM replayed
    WHERE observable_bucket = {{ country_absent_sentinel() }}
      AND LENGTH(observable_bucket) = 2
),

-- (f) THE CALL SITES. Vacuous on a warehouse with no country rows and exact on one
-- that has them: no country row of the fact may carry a NULL value, and the country
-- partitions must total their own staging. These are the two statements the replay
-- above cannot make, kept here rather than dropped.
null_country_value AS (
    SELECT
        connector,
        metric,
        CAST(NULL AS {{ toorow_string_type() }})                            AS bucket,
        CAST(NULL AS {{ toorow_float_type() }})                             AS measured_total,
        CAST(NULL AS {{ toorow_float_type() }})                             AS expected_total,
        'NULL_BREAKDOWN_VALUE: a country row of fact_daily_kpi carries no value'
                                                         AS failure_reason
    FROM {{ ref('fact_daily_kpi') }}
    WHERE breakdown_dimension = 'country'
      AND breakdown_value IS NULL
),

staged_totals AS (
    SELECT project_id, date, 'cm360' AS connector, metric,
           SUM(CAST(value AS {{ toorow_float_type() }})) AS staged_total
    FROM {{ ref('stg_cm360_daily') }}
    WHERE non_additive = FALSE
    GROUP BY project_id, date, metric

    UNION ALL

    SELECT project_id, date, 'dv360' AS connector, metric,
           SUM(CAST(value AS {{ toorow_float_type() }})) AS staged_total
    FROM {{ ref('stg_dv360_daily') }}
    WHERE non_additive = FALSE
    GROUP BY project_id, date, metric

    UNION ALL

    SELECT project_id, date, 'taboola' AS connector, metric,
           SUM(CAST(value AS {{ toorow_float_type() }})) AS staged_total
    FROM {{ ref('stg_taboola_daily') }}
    WHERE non_additive = FALSE
    GROUP BY project_id, date, metric
),

partition_totals AS (
    SELECT project_id, date, connector, metric, SUM(value) AS country_total
    FROM {{ ref('int_country_daily_kpi') }}
    GROUP BY project_id, date, connector, metric
),

partition_lost AS (
    SELECT
        s.connector,
        s.metric,
        CAST(NULL AS {{ toorow_string_type() }})                            AS bucket,
        p.country_total                                  AS measured_total,
        s.staged_total                                   AS expected_total,
        'PARTITION_TOTAL_LOST: int_country_daily_kpi does not total its staging'
                                                         AS failure_reason
    FROM staged_totals s
    LEFT JOIN partition_totals p
        ON p.project_id = s.project_id AND p.date = s.date
       AND p.connector = s.connector AND p.metric = s.metric
    WHERE s.staged_total IS NOT NULL
      AND (p.country_total IS NULL OR ABS(p.country_total - s.staged_total) > 0.001)
)

SELECT * FROM cardinality_guard
UNION ALL
SELECT * FROM absent_not_bucketed
UNION ALL
SELECT * FROM unresolved_swallowed
UNION ALL
SELECT * FROM lost_rows
UNION ALL
SELECT * FROM bucket_looks_like_a_country
UNION ALL
SELECT * FROM null_country_value
UNION ALL
SELECT * FROM partition_lost
