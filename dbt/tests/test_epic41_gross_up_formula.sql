-- T5 (Story 41.3, AC6) -- the WHT gross-up formula, exact and FAIL-CLOSED.
--
-- Asserts, on fixture scenario S7 (wht_gross_up) replayed through the real macro:
--   * grossed = ROUND(base / (1 - w)) with ONE rounding: 13 097 521 303 / 0.85
--     = 15 408 848 591.7647... -> 15 408 848 592;
--   * component = grossed - base by EXACT INTEGER SUBTRACTION = 2 311 327 289;
--   * w = 0 -> component exactly 0, with no special case (1 - 0 = 1);
--   * w >= 1 -> component NULL, never a division by zero, never an infinity;
--   * (F10) an IN-RANGE w whose quotient would overflow INT64 -> a typed gap on the live
--     ladder, not the DuckDB conversion error that used to abort the whole build.
-- `base + component == grossed` is NOT asserted: it is integer algebra and cannot fail
-- (review finding F3). It is guaranteed by construction -- that is precisely why the
-- macro returns the grossed TOTAL and the caller subtracts.
-- And, on the live ladder, that a rule with w = 1.000000 produces
-- wht_gross_up_micros NULL + gap_wht_rate_invalid + NULL headline totals rather than
-- an error or a fabricated number. Postgres refuses w >= 1 at declaration
-- (migration 119's ck_fee_tax_rules_gross_up_rate), so the DuckDB dev mirror is the
-- ONLY place this guard can be exercised at all -- which is exactly why the seeder
-- lands it.
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
WITH s7 AS (
    SELECT date, net_media_micros, rule_rate, expected_wht_gross_up_micros
    FROM {{ ref('epic41_cascade_fixture') }}
    WHERE scenario = 'wht_gross_up'
),

cardinality_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: fixture scenario wht_gross_up is missing' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM s7) = 0
),

anti_vacuity_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'ANTI_VACUITY_FAIL: fixture scenario wht_gross_up carries no rate >= 1 row, so'
        || ' the fail-closed division guard is not exercised' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM s7 WHERE CAST(rule_rate AS {{ toorow_float_type() }}) >= 1) = 0
),

replayed AS (
    SELECT
        s.date                                                              AS date,
        s.net_media_micros                                                  AS base_micros,
        s.rule_rate                                                         AS rate,
        s.expected_wht_gross_up_micros                                      AS expected_component,
        {{ fee_tax_grossup_micros('s.net_media_micros', 's.rule_rate') }}   AS grossed_micros,
        {{ fee_tax_grossup_micros('s.net_media_micros', "'0.000000'") }}    AS grossed_at_zero
    FROM s7 s
),

valid_rate_breaks AS (
    SELECT
        CAST(r.date AS STRING) AS subject,
        'GROSSUP_FAIL: component ' || CAST(r.grossed_micros - r.base_micros AS STRING)
            || ' <> expected ' || CAST(r.expected_component AS STRING) AS failure_reason
    FROM replayed r
    WHERE CAST(r.rate AS {{ toorow_float_type() }}) < 1
      AND r.grossed_micros - r.base_micros <> r.expected_component
    -- REMOVED after review finding F3: `base + (grossed - base) <> grossed` is integer
    -- algebra, true for every BIGINT triple, and cannot fail. The property is real and
    -- load-bearing -- it is WHY the macro returns the grossed TOTAL and the caller
    -- subtracts -- but it is guaranteed by CONSTRUCTION, not by a test, and pretending
    -- otherwise inflated the suite's apparent coverage. The assertion above (component
    -- == the pinned 2 311 327 289) is the one that does the work.
    UNION ALL
    SELECT
        CAST(r.date AS STRING),
        'GROSSUP_FAIL: w = 0 must leave the base untouched (component exactly 0)'
    FROM replayed r
    WHERE r.grossed_at_zero <> r.base_micros
),

invalid_rate_breaks AS (
    SELECT
        CAST(r.date AS STRING) AS subject,
        'GROSSUP_FAIL: w >= 1 must yield NULL, never a number and never a division by zero'
            AS failure_reason
    FROM replayed r
    WHERE CAST(r.rate AS {{ toorow_float_type() }}) >= 1
      AND r.grossed_micros IS NOT NULL
),

live_breaks AS (
    SELECT
        l.project_id || '|' || CAST(l.date AS STRING) || '|' || l.breakdown_value AS subject,
        'GROSSUP_FAIL: an invalid WHT rate did not produce NULL + gap_wht_rate_invalid'
        || ' + NULL headline totals on the live ladder' AS failure_reason
    FROM {{ ref('fee_tax_ladder_daily') }} l
    WHERE l.project_id = 'feetax_dev_wht_bad'
      AND NOT (l.wht_gross_up_micros IS NULL
               AND l.gap_wht_rate_invalid
               AND l.subtotal_ht_micros IS NULL
               AND l.total_ttc_micros IS NULL)
),

overflow_breaks AS (
    -- F10. rate = 0.999999 is ADMITTED by migration 119's CHECK, and on a 20 M EUR base
    -- the quotient exceeds INT64. Before the macro's magnitude guard DuckDB raised
    -- `Conversion Error: Type DOUBLE with value 1e+19 ... out of range for INT64` and
    -- took the BUILD DOWN -- a red build where E41-NFR02 requires a typed gap. The fact
    -- that this test can be EVALUATED at all is half the assertion.
    SELECT
        l.project_id || '|' || CAST(l.date AS STRING) AS subject,
        'GROSSUP_OVERFLOW_FAIL: an in-range rate over an INT64-overflowing base must'
        || ' produce gap_wht_base_overflow + NULL totals, not a number and not a build'
        || ' failure. Got gap_codes=' || l.gap_codes AS failure_reason
    FROM {{ ref('fee_tax_ladder_daily') }} l
    WHERE l.project_id = 'feetax_dev_wht_overflow'
      AND NOT (l.gap_wht_base_overflow
               AND l.gap_codes = 'WHT_BASE_OVERFLOW'
               AND l.wht_gross_up_micros IS NULL
               AND l.total_ttc_micros IS NULL
               -- and the rule must NOT be advertised as applied while contributing 0
               AND l.applied_rule_ids = '')
),

overflow_cardinality_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: no ladder row for feetax_dev_wht_overflow -- the INT64'
        || ' magnitude guard is untested' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (
        SELECT COUNT(*) FROM {{ ref('fee_tax_ladder_daily') }}
        WHERE project_id = 'feetax_dev_wht_overflow'
    ) = 0
)

SELECT subject, failure_reason FROM cardinality_guard
UNION ALL SELECT subject, failure_reason FROM anti_vacuity_guard
UNION ALL SELECT subject, failure_reason FROM valid_rate_breaks
UNION ALL SELECT subject, failure_reason FROM invalid_rate_breaks
UNION ALL SELECT subject, failure_reason FROM live_breaks
UNION ALL SELECT subject, failure_reason FROM overflow_cardinality_guard
UNION ALL SELECT subject, failure_reason FROM overflow_breaks
{%- endif -%}
