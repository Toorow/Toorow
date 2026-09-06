-- T19 (Story 41.3, AC7 / C.6 / scenario S9) -- the micros boundary is exact and
-- happens ONCE PER SOURCE ROW.
--
-- A NINETEENTH singular test where the story's table lists eighteen: scenario S9 is
-- named in the story's Invariants ("Micros-exact, sum-then-round never round-then-sum:
-- T4, T3, S9") and in the DoD's anti-vacuity list, but no T-number was assigned to it.
-- Rather than bolt it onto an unrelated test, it gets its own file. Recorded as a
-- deviation in the Dev Agent Record.
--
-- The rule the engine must obey: normalise PER SOURCE ROW with fee_tax_to_micros, THEN
-- SUM exact BIGINTs. `ROUND(SUM(value) * 1e6)` would put the single rounding boundary
-- AFTER the float drift instead of before it, and it is also not what
-- server/core/money.py::to_canonical_micros does -- C.6 requires the SQL to mirror the
-- Python adapter, which rounds each value.
--
-- Asserted:
--   * fee_tax_to_micros(source_decimal_value) equals the pinned micros for EVERY row --
--     the boundary itself is exact, one value at a time;
--   * the exact integer SUM of the two S9 rows is 15 555 554 000 010;
--   * on the LIVE ladder, SUM(net_media_micros) over a (project, date, connector)
--     equals the per-row-normalised sum of the canonical series, exactly. (The
--     complementary "strictly less than the all-dimensions total" half lives in T10.)
--
-- ANTI-VACUITY GUARD, and an honest note on its shape: the epic-39 fixture guards
-- against a DIVIDE-then-sum drift on near-2^53 magnitudes, and that guard does not
-- transpose here. The boundary this story moves is round-then-sum vs sum-then-round,
-- and at the magnitudes a real media budget reaches, IEEE754 accumulation happens to
-- agree with exact decimal -- so a "the naive path MUST drift" guard would be a false
-- alarm generator, not a proof. The guard asserted instead is structural and is the one
-- that actually matters: the fixture must carry AT LEAST TWO rows on the SAME
-- (project, date, connector) whose micros are NOT round, so a sum is genuinely
-- exercised and a regression to a single-row fixture is caught.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

WITH s9 AS (
    SELECT project_id, connector, date, source_decimal_value, net_media_micros
    FROM {{ ref('epic41_cascade_fixture') }}
    WHERE scenario = 'micros_exactness'
),

cardinality_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: fixture scenario micros_exactness is missing' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM s9) = 0
),

anti_vacuity_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'ANTI_VACUITY_FAIL: micros_exactness no longer carries >= 2 non-round-micros rows'
        || ' on the same (project, date, connector), so no SUM is exercised and the'
        || ' per-source-row boundary is untested' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (
        SELECT COUNT(*)
        FROM (
            SELECT project_id, date, connector
            FROM s9
            -- `%` was here : l operateur modulo est du DuckDB/Postgres, BigQuery
            -- repond *Syntax error: Expected ")" but got "%"*, donc ce test etait
            -- REFUSE sur le moteur du nocturne (mesure 2026-08-24, dry run a 0
            -- octet). `MOD()` est la forme que les deux moteurs portent. AI-314.
            WHERE MOD(net_media_micros, 1000000) <> 0
            GROUP BY project_id, date, connector
            HAVING COUNT(*) >= 2
        ) g
    ) = 0
),

boundary_breaks AS (
    SELECT
        s.project_id || '|' || CAST(s.date AS STRING) || '|'
            || CAST(s.source_decimal_value AS STRING) AS subject,
        'MICROS_BOUNDARY_FAIL: fee_tax_to_micros gave '
            || CAST({{ fee_tax_to_micros('s.source_decimal_value') }} AS STRING)
            || ' but the pinned value is ' || CAST(s.net_media_micros AS STRING)
            AS failure_reason
    FROM s9 s
    WHERE {{ fee_tax_to_micros('s.source_decimal_value') }} <> s.net_media_micros
),

sum_break AS (
    SELECT
        'micros_exactness' AS subject,
        'MICROS_SUM_FAIL: the exact integer sum of the S9 rows is '
            || CAST(SUM(s.net_media_micros) AS STRING)
            || ', expected 15555554000010' AS failure_reason
    FROM s9 s
    GROUP BY s.project_id, s.date, s.connector
    HAVING SUM(s.net_media_micros) <> 15555554000010
),

live_base_break AS (
    SELECT
        l.project_id || '|' || CAST(l.date AS STRING) || '|' || l.connector AS subject,
        'MICROS_BASE_FAIL: the ladder base is not the per-source-row-normalised sum of'
        || ' the canonical cost series' AS failure_reason
    FROM (
        SELECT project_id, date, connector, breakdown_dimension,
               SUM(net_media_micros) AS ladder_micros
        FROM {{ ref('fee_tax_ladder_daily') }}
        GROUP BY project_id, date, connector, breakdown_dimension
    ) l
    JOIN (
        SELECT f.project_id, CAST(f.date AS DATE) AS date, f.connector,
               f.breakdown_dimension,
               SUM({{ fee_tax_to_micros('f.value') }}) AS fact_micros
        FROM {{ ref('fact_daily_kpi') }} f
        WHERE f.metric = 'cost'
        GROUP BY f.project_id, CAST(f.date AS DATE), f.connector, f.breakdown_dimension
    ) k
        ON  k.project_id          = l.project_id
        AND k.date                = l.date
        AND k.connector           = l.connector
        AND k.breakdown_dimension = l.breakdown_dimension
    WHERE l.ladder_micros <> k.fact_micros
)

SELECT subject, failure_reason FROM cardinality_guard
UNION ALL SELECT subject, failure_reason FROM anti_vacuity_guard
UNION ALL SELECT subject, failure_reason FROM boundary_breaks
UNION ALL SELECT subject, failure_reason FROM sum_break
UNION ALL SELECT subject, failure_reason FROM live_base_break
