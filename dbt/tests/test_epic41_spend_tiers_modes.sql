-- T6 (Story 41.3, AC5 / E41-NFR03 / decision D4) -- SPEND_TIERS, BOTH modes, at view time.
--
-- The same three days and the SAME bands [(0, 5 %), (20 000 EUR, 3 %)] are seeded twice,
-- once with mode='cliff' and once with mode='marginal', because the three candidate
-- answers for day 3 are ALL DIFFERENT and only asserting them separately pins the mode
-- AND the frame independently:
--     cliff, inclusive frame    -> 210 000 000   (the whole day at the new 3 % rate)
--     marginal, inclusive frame -> 270 000 000   (fee(24e9) 1 120 000 000 - fee(17e9) 850 000 000)
--     cliff, EXCLUSIVE frame    -> 350 000 000   (the crossing day still at 5 %) -- WRONG
-- The expected values live in epic41_cascade_fixture (scenarios spend_tiers_cliff /
-- spend_tiers_marginal) and are asserted against the LIVE fee_tax_ladder_daily, so this
-- test exercises the real SUM() OVER window, not a replay.
--
-- Also asserts that a SPEND_TIERS rule with no usable band ladder raises
-- gap_tiers_malformed rather than silently contributing 0.
--
-- CARDINALITY + ANTI-VACUITY GUARDS: both tier projects must produce three ladder rows
-- each, and the cliff and marginal answers for day 3 must actually DIFFER -- if they
-- agree, the fixture no longer discriminates between the modes and this test proves
-- nothing.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

{#- UNE PREMISSE ABSENTE N EST PAS UN DEFAUT (AI-314, 2026-08-24).
    Ce test epingle un exemple SEME : il mesure ce que la fixture locale porte, et
    la fixture vit dans le MIROIR. `mirror_sync` differe ses ecritures BigQuery
    (Phase B), donc dans un entrepot ou le miroir n a pas ete ecrit -- toute la
    production aujourd hui -- ce test ne trouve rien a mesurer et rend son
    CARDINALITY_FAIL : un rouge qui accuse le calcul d un defaut dont la cause est
    qu il n y a rien a calculer. Un test rouge est un code de sortie, et un code
    de sortie est un projet sans marts.
    Il DECLINE donc de juger, EN LE DISANT : `TOOROW_SOURCE_ABSENT` remonte au
    nocturne, qui refuse alors le mot << ok >> pour ce projet. La ou le miroir EST
    -- la boucle locale, la CI -- rien ne bouge et l assertion reste entiere. -#}
{%- set mirror_missing = toorow_absent_sources('mirror', ['project_tax_fee_activation']) -%}
{%- if mirror_missing | length > 0 %}
{{ toorow_absent_source_stub('mirror', mirror_missing, [['declined', 'string']]) }}
{%- else %}
WITH expected AS (
    SELECT
        CASE scenario
            WHEN 'spend_tiers_cliff'    THEN 'feetax_dev_tiers_cliff'
            ELSE 'feetax_dev_tiers_marginal'
        END                                     AS project_id,
        date                                    AS date,
        MAX(expected_platform_fee_micros)       AS expected_component
    FROM {{ ref('epic41_cascade_fixture') }}
    WHERE scenario IN ('spend_tiers_cliff', 'spend_tiers_marginal')
    GROUP BY scenario, date
),

-- DAY-LEVEL, since review finding F9. feetax_dev_tiers_marginal now carries TWO
-- campaigns a day: with one row per day the row-level cumulative degenerated to the
-- day-level one, `tranche_excl` never differed from it, and the marginal telescoping --
-- the whole reason the row-level window exists -- was never exercised. The pinned
-- expectations are per DAY, which is also the grain the ruling is stated at.
live_rows AS (
    SELECT project_id, date, platform_fee_micros
    FROM {{ ref('fee_tax_ladder_daily') }}
    WHERE project_id IN ('feetax_dev_tiers_cliff', 'feetax_dev_tiers_marginal')
),

live AS (
    SELECT
        project_id, date,
        SUM(platform_fee_micros) AS platform_fee_micros,
        COUNT(*)                 AS n_rows
    FROM live_rows
    GROUP BY project_id, date
),

cardinality_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: expected 3 tier DAYS per project (6 total), got '
        || CAST((SELECT COUNT(*) FROM live) AS STRING)
        || ' -- run seed_fee_tax_mirror.py before dbt build' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM live) <> 6
),

-- F9: the marginal mode must be exercised with MORE THAN ONE ROW PER DAY, or the
-- row-level cumulative collapses onto the day-level one and the telescoping identity is
-- never tested. The reviewer re-derived the expected split by hand: 150/250, 200/250,
-- 150/120 M -> days 400/450/270 M -> period 1 120 M.
row_cardinality_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'ANTI_VACUITY_FAIL: feetax_dev_tiers_marginal has at most one ladder row per day,'
        || ' so the ROW-LEVEL cumulative degenerates to the day-level one and the'
        || ' marginal telescoping is untested' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (
        SELECT COALESCE(MAX(n_rows), 0) FROM live
        WHERE project_id = 'feetax_dev_tiers_marginal'
    ) < 2
),

period_total_guard AS (
    -- The telescoping identity end to end: the whole period's marginal fee must equal
    -- fee(24e9) = 0.05 x 20e9 + 0.03 x 4e9 = 1 120 000 000, with no allocation residue
    -- from having split the days across campaigns.
    SELECT
        'feetax_dev_tiers_marginal' AS subject,
        'TELESCOPING_FAIL: the marginal period total is '
        || CAST((SELECT COALESCE(SUM(platform_fee_micros), -1) FROM live
                 WHERE project_id = 'feetax_dev_tiers_marginal') AS STRING)
        || ', expected 1120000000 -- the per-row differences no longer telescope'
            AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COALESCE(SUM(platform_fee_micros), -1) FROM live
           WHERE project_id = 'feetax_dev_tiers_marginal') <> 1120000000
),

anti_vacuity_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'ANTI_VACUITY_FAIL: cliff and marginal give the SAME day-3 component, so the'
        || ' fixture no longer discriminates between the two modes' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (
        SELECT COUNT(DISTINCT platform_fee_micros)
        FROM live
        WHERE date = DATE '2026-05-03'
    ) < 2
),

exclusive_frame_guard AS (
    -- The frame, pinned negatively: 350 000 000 is what an EXCLUSIVE frame would give
    -- on day 3 (the crossing day still at 5 %). It must appear nowhere.
    SELECT
        l.project_id || '|' || CAST(l.date AS STRING) AS subject,
        'FRAME_FAIL: day 3 produced 350000000, which is the EXCLUSIVE-frame answer --'
        || ' the tier frame must be ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW'
            AS failure_reason
    FROM live l
    WHERE l.date = DATE '2026-05-03'
      AND l.platform_fee_micros = 350000000
),

pinned_breaks AS (
    SELECT
        l.project_id || '|' || CAST(l.date AS STRING) AS subject,
        'TIERS_FAIL: expected ' || CAST(e.expected_component AS STRING)
            || ' got ' || COALESCE(CAST(l.platform_fee_micros AS STRING), 'NULL')
            AS failure_reason
    FROM live l
    JOIN expected e
        ON  e.project_id = l.project_id
        AND e.date       = l.date
    WHERE l.platform_fee_micros IS DISTINCT FROM e.expected_component
),

malformed_breaks AS (
    -- A SPEND_TIERS rule with no band ladder at all must gap, never silently add 0.
    SELECT
        l.project_id || '|' || CAST(l.date AS STRING) AS subject,
        'TIERS_FAIL: a SPEND_TIERS rule with no band raised no gap_tiers_malformed --'
        || ' a malformed ladder must never be a silent 0' AS failure_reason
    FROM {{ ref('fee_tax_ladder_daily') }} l
    WHERE l.project_id = 'feetax_dev_tiers_bad'
      AND NOT (l.gap_tiers_malformed
               AND l.platform_fee_micros IS NULL
               AND l.total_ttc_micros IS NULL)
)

SELECT subject, failure_reason FROM cardinality_guard
UNION ALL SELECT subject, failure_reason FROM row_cardinality_guard
UNION ALL SELECT subject, failure_reason FROM period_total_guard
UNION ALL SELECT subject, failure_reason FROM anti_vacuity_guard
UNION ALL SELECT subject, failure_reason FROM exclusive_frame_guard
UNION ALL SELECT subject, failure_reason FROM pinned_breaks
UNION ALL SELECT subject, failure_reason FROM malformed_breaks
{%- endif -%}
