-- Story 41.3 -- THE GROSS-UP FAIL-CLOSED GUARD, EXERCISED ON THE MACRO ITSELF.
--
-- WHY THIS TEST EXISTS (review finding D, 2026-08-10). `fee_tax_grossup_micros`
-- (dbt/macros/fee_tax_money_math.sql:106) refuses `rate IS NULL OR rate >= 1 OR
-- rate < 0`, and its docstring calls that a FAIL-CLOSED GUARD, "inside the macro so a
-- caller cannot forget them". Deleting `rate >= 1 OR rate < 0` and running a full
-- `dbt build` produced PASS=58 ERROR=0: the only red was
-- `test_forbidden_files_are_unchanged_from_head`, which reddens for ANY edit to that file
-- and therefore proves nothing about the property. A text ratchet is not a behaviour
-- guard.
--
-- The reason no existing test could fall is that every caller PRE-FILTERS. The ladder
-- applies the macro only where `m.wht_invalid = 0` (fee_tax_ladder_daily.sql:793), so an
-- invalid rate never reaches it through the model. This test therefore calls the macro
-- DIRECTLY, on literal (base, rate) pairs, outside any caller -- which is the only place
-- the guard's own behaviour is observable.
--
-- ===================== WHAT THE MUTATIONS ACTUALLY MEASURE ====================
-- Measured on DuckDB, 2026-08-10, with this test in place, mutating the two clauses
-- separately -- because they are NOT equally load-bearing and saying otherwise would be
-- the same untested claim again. `dbt build --select test_epic41_grossup_guard_direct`:
--
--     rate < 0 removed        -> Got 2 results  Done. PASS=0 ERROR=1
--     rate >= 1 removed       -> Got 0 results  Done. PASS=1 ERROR=0
--     both removed (review D) -> Got 2 results  Done. PASS=0 ERROR=1
--
--   * `rate < 0` REMOVED  -> RED. At rate = -0.500000 the magnitude guard cannot fire
--     (its threshold becomes ~1.4e19, above BIGINT range), so the ELSE branch runs and
--     returns base / 1.5 -- a grossed total SMALLER than the base, i.e. a NEGATIVE
--     withholding component on an invoice.
--   * `rate >= 1` REMOVED -> STILL GREEN, and this is a measured fact rather than an
--     oversight: for rate >= 1 the magnitude guard's threshold
--     (1000000 - rate*1000000) * 9223372036000 is <= 0, so every positive base trips it
--     and the answer is NULL anyway. The property is held TWICE. The clause is kept
--     because the second holder is an overflow guard whose bound is about capacity, not
--     about the arithmetic being meaningless -- and because BigQuery's NUMERIC branch
--     must not be argued about from a DuckDB measurement.
--
-- So this test asserts the PROPERTY at all three boundaries and can genuinely fall at
-- one of them. That is a strictly stronger statement than the file-hash ratchet it
-- complements, and its comment says exactly how strong.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

{#- `VALUES` AS A STANDALONE TABLE DOES NOT CROSS (AI-314, 2026-08-24): BigQuery
    answers *Expected keyword JOIN but got ","*, so this test was REFUSED every
    night on the engine production runs (measured with a dry run, 0 bytes). The
    four rows are the same rows, spelled as a UNION of one-row SELECTs, which
    both engines carry.

    The decimal width was the half that stayed open that day, and it is closed
    here: `DECIMAL(10, 6)` is now `{{ toorow_decimal_type(10, 6) }}`, which emits
    `NUMERIC` on BigQuery -- exactly DECIMAL(38, 9), wide enough to hold every
    value of DECIMAL(10, 6) without narrowing -- and `DECIMAL(10, 6)` on DuckDB.
    A bare `DECIMAL(p, s)` in a CAST is what BigQuery refuses with *Parameterized
    types are not allowed in CAST expressions*, and dbt's own `type_numeric()`
    renders `NUMERIC(28, 6)`, just as parameterised. The rule now lives once, in
    `dbt/macros/engine_types.sql`, and covers every relation and test in the
    project rather than this file alone (execution-substrate "Incomplete if" 23). #}
WITH probe AS (
    -- label, base_micros, rate
    SELECT 'rate_exactly_one' AS label,
           CAST(1000000 AS BIGINT) AS base_micros,
           CAST(1.000000 AS {{ toorow_decimal_type(10, 6) }}) AS rate
    UNION ALL SELECT 'rate_above_one', CAST(1000000 AS BIGINT),
                     CAST(1.500000 AS {{ toorow_decimal_type(10, 6) }})
    -- THE CLAUSE THAT ONLY THIS TEST HOLDS. A negative withholding rate is the
    -- one invalid input the magnitude guard cannot catch.
    UNION ALL SELECT 'rate_negative', CAST(1000000 AS BIGINT),
                     CAST(-0.500000 AS {{ toorow_decimal_type(10, 6) }})
    UNION ALL SELECT 'rate_null', CAST(1000000 AS BIGINT),
                     CAST(NULL AS {{ toorow_decimal_type(10, 6) }})
),

evaluated AS (
    SELECT
        p.label                                                        AS label,
        p.base_micros                                                  AS base_micros,
        p.rate                                                         AS rate,
        {{ fee_tax_grossup_micros('p.base_micros', 'p.rate') }}        AS grossed_micros
    FROM probe p
),

-- Every invalid rate must return NULL. Not 0 (a 0 grossed total would ASSERT the payer
-- owes nothing), not a number, not a raised error.
not_refused AS (
    SELECT
        e.label AS subject,
        'GROSSUP_GUARD_FAIL: fee_tax_grossup_micros returned '
        || CAST(e.grossed_micros AS STRING)
        || ' for an INVALID rate (' || CAST(e.rate AS STRING) || ') instead of NULL.'
        || ' rate >= 1 makes 1 - rate zero or negative and rate < 0 makes the quotient'
        || ' SMALLER than the base -- a negative withholding component on an invoice.'
        || ' E41-NFR02 requires a typed refusal, which the caller turns into'
        || ' gap_wht_rate_invalid.' AS failure_reason
    FROM evaluated e
    WHERE e.grossed_micros IS NOT NULL
),

-- The specific shape of the rate < 0 failure, named separately so a red says WHAT went
-- wrong rather than only that something did.
negative_grossup AS (
    SELECT
        e.label AS subject,
        'NEGATIVE_GROSSUP_FAIL: the grossed total ('
        || CAST(e.grossed_micros AS STRING) || ') is BELOW the base ('
        || CAST(e.base_micros AS STRING) || '). A withholding gross-up can only ever'
        || ' RAISE the amount owed; a grossed total under the base means the component'
        || ' base - grossed is negative money.' AS failure_reason
    FROM evaluated e
    WHERE e.grossed_micros IS NOT NULL
      AND e.grossed_micros < e.base_micros
),

-- ANTI-VACUITY. A valid rate on the same shape of call MUST return a number, or the
-- assertions above would also hold if the macro returned NULL for everything.
valid_rate_probe AS (
    SELECT
        {{ fee_tax_grossup_micros('CAST(1000000 AS BIGINT)', "CAST(0.100000 AS " ~ toorow_decimal_type(10, 6) ~ ")") }}
            AS grossed_micros
),

vacuity_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'ANTI_VACUITY_FAIL: fee_tax_grossup_micros returned NULL for the VALID rate'
        || ' 0.100000 over 1 000 000 micros (expected 1 111 111), so "invalid rates'
        || ' return NULL" holds only because the macro returns NULL for everything.'
            AS failure_reason
    FROM valid_rate_probe v
    WHERE v.grossed_micros IS DISTINCT FROM 1111111
)

SELECT subject, failure_reason FROM not_refused
UNION ALL SELECT subject, failure_reason FROM negative_grossup
UNION ALL SELECT subject, failure_reason FROM vacuity_guard
