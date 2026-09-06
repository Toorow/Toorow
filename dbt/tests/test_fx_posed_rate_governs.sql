-- test_fx_posed_rate_governs.sql -- Story 67.13, the warehouse half of the FX cutover.
--
-- WHAT THIS FILE IS FOR. `docs/product-architecture/capabilities/currency-fx.md`
-- admits, in step 3 of the cutover it ratifies, that the resolution order now
-- exists TWICE -- once in `core.fx_rate_sets._resolve_posed` and once in
-- `dbt/macros/fx_posed_resolution.sql` -- and it names what makes that
-- admissible rather than merely tolerated:
--
--     "the precedent is dbt/macros/fee_tax_condition_matcher.sql standing beside
--      core.tax_fee_rule_set, and what that precedent carries is a DIFFERENTIAL
--      ORACLE, not two half-tested engines."
--
-- This is the warehouse side of that oracle: the same five cases, posed to the
-- SQL engine. The application side is
-- `server/tests/integration/test_fx_conditional_rate_postgres.py`, and
-- `server/tests/core/test_fx_warehouse_resolution_matches_engine.py` is what
-- refuses the two from drifting apart.
--
-- WHY A SEED FIXTURE AND NOT A MIRROR ROW. The macro reads
-- `source('mirror', 'fx_posed_rates')` in production and a singular test cannot
-- write into the mirror. Posting a rate into the local fixture's mirror instead
-- would move every converted total of the warehouse -- which is precisely what
-- `test_fx_at_read_totals_unchanged` and `test_epic39_totals_bit_identical` pin:
-- for a Project that has posed no rate, this cutover must be a NO-OP. So the
-- engine is judged here, through the macro's `rates_relation` /
-- `conditions_relation` seams, and the marts are judged there, on emptiness.
-- `dbt seed` loads the fixture in every environment, so the evidence travels
-- with the repository instead of depending on a script somebody has to run.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

{% set rates = ref('fx_posed_rates_fixture') %}
{% set conds = ref('fx_posed_rate_conditions_fixture') %}

WITH resolved AS (
    -- The SAME macro the thirteen staging models call, asked for two different
    -- connectors. Two calls, because `connector` is the one condition key a
    -- staging row can answer and its answer is what the parameter carries.
    SELECT 'meta-ads' AS connector, r.*
    FROM {{ toorow_fx_posed_resolution('meta-ads', rates, conds) }} r
    UNION ALL
    SELECT 'stripe' AS connector, r.*
    FROM {{ toorow_fx_posed_resolution('stripe', rates, conds) }} r
),

-- Every expectation, as data. `expect_rows = 0` is not an absence of assertion:
-- it is the assertion that NOTHING is served, so the caller falls back to the
-- seed -- which is a different outcome from a refusal and must not be confused
-- with one.
probe AS (
    SELECT 'plain_meta'        AS case_name, 'fxp_plain'      AS project_id,
           DATE '2026-07-15'   AS on_date,   'meta-ads'       AS connector,
           CAST(0.500000 AS {{ toorow_decimal_type(38, 9) }}) AS expect_rate,
           CAST(NULL AS {{ dbt.type_string() }}) AS expect_gap, 1 AS expect_rows,
           'an unconditional posed rate governs its window'  AS meaning
    UNION ALL
    -- SPECIFICITY: the conditional rate outranks the unconditional one it
    -- refines, exactly when it matches.
    SELECT 'specific_meta', 'fxp_specific', DATE '2026-07-15', 'meta-ads',
           CAST(0.600000 AS {{ toorow_decimal_type(38, 9) }}), CAST(NULL AS {{ dbt.type_string() }}), 1,
           'the more specific matching rule wins'
    UNION ALL
    -- ... and the unconditional one is the FALLBACK, not a rival: for a
    -- connector the conditional rule does not name, it is simply NO_MATCH.
    SELECT 'specific_stripe', 'fxp_specific', DATE '2026-07-15', 'stripe',
           CAST(0.500000 AS {{ toorow_decimal_type(38, 9) }}), CAST(NULL AS {{ dbt.type_string() }}), 1,
           'a rule that names another connector is NO_MATCH, and the unconditional rate serves'
    UNION ALL
    -- A TIE IS REFUSED, NEVER BROKEN QUIETLY (bullet 10). A rate picked by row
    -- order restates every monetary figure it touches and leaves nothing to
    -- notice it by.
    SELECT 'tie_meta', 'fxp_tie', DATE '2026-07-15', 'meta-ads',
           CAST(NULL AS {{ toorow_decimal_type(38, 9) }}), 'fx_condition_conflict', 1,
           'two equally specific rules match: no rate is served and the conflict is named'
    UNION ALL
    -- The same two rules, for a connector neither names: both NO_MATCH, no
    -- candidate, no segment. Not a refusal -- an absence, and the seed converts.
    SELECT 'tie_stripe', 'fxp_tie', DATE '2026-07-15', 'stripe',
           CAST(NULL AS {{ toorow_decimal_type(38, 9) }}), CAST(NULL AS {{ dbt.type_string() }}), 0,
           'a tie between rules that do not match this connector is not a tie at all'
    UNION ALL
    -- UNRESOLVED BEATS NO_MATCH AND BEATS THE FALLBACK (bullet 11). A staging row
    -- carries no country, so a rate conditioned on one might apply; serving the
    -- unconditional rate beside it would be the reassuring answer this capability
    -- refuses.
    SELECT 'unresolved_meta', 'fxp_unresolved', DATE '2026-07-15', 'meta-ads',
           CAST(NULL AS {{ toorow_decimal_type(38, 9) }}), 'fx_condition_unresolved', 1,
           'a condition this grain cannot answer refuses, and does not fall through'
    UNION ALL
    SELECT 'unresolved_stripe', 'fxp_unresolved', DATE '2026-07-15', 'stripe',
           CAST(NULL AS {{ toorow_decimal_type(38, 9) }}), 'fx_condition_unresolved', 1,
           'the refusal does not depend on the connector, because the key does not'
    UNION ALL
    -- THE WINDOW IS THE ONLY TEMPORAL AUTHORITY (Wording A, and bullet 7).
    SELECT 'window_inside', 'fxp_window', DATE '2026-07-05', 'meta-ads',
           CAST(0.500000 AS {{ toorow_decimal_type(38, 9) }}), CAST(NULL AS {{ dbt.type_string() }}), 1,
           'a day inside the declared window resolves the posed rate'
    UNION ALL
    SELECT 'window_last_day', 'fxp_window', DATE '2026-07-10', 'meta-ads',
           CAST(0.500000 AS {{ toorow_decimal_type(38, 9) }}), CAST(NULL AS {{ dbt.type_string() }}), 1,
           'valid_to is INCLUSIVE -- the half-open segment must not drop its last day'
    UNION ALL
    SELECT 'window_after', 'fxp_window', DATE '2026-07-20', 'meta-ads',
           CAST(NULL AS {{ toorow_decimal_type(38, 9) }}), CAST(NULL AS {{ dbt.type_string() }}), 0,
           'a posed rate does not outlive its own window'
    UNION ALL
    SELECT 'window_before', 'fxp_window', DATE '2026-06-20', 'meta-ads',
           CAST(NULL AS {{ toorow_decimal_type(38, 9) }}), CAST(NULL AS {{ dbt.type_string() }}), 0,
           'nor does it reach back before it'
),

-- (a) ANTI-VACUITY. Without the fixture every assertion below is a zero-row pass.
cardinality_guard AS (
    SELECT 'CARDINALITY'                                  AS case_name,
           CAST(NULL AS {{ toorow_decimal_type(38, 9) }})                   AS expect_rate,
           CAST(NULL AS {{ toorow_decimal_type(38, 9) }})                   AS got_rate,
           CAST(NULL AS {{ dbt.type_string() }})          AS expect_gap,
           CAST(NULL AS {{ dbt.type_string() }})          AS got_gap,
           0                                              AS expect_rows,
           0                                              AS got_rows,
           'CARDINALITY_FAIL: fx_posed_rates_fixture does not carry all five '
           || 'scenarios -- the seed did not run, or a case was deleted'
                                                          AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(DISTINCT scenario) FROM {{ rates }}) <> 5
),

matched AS (
    SELECT
        p.case_name, p.expect_rate, p.expect_gap, p.expect_rows, p.meaning,
        COUNT(r.project_id)                     AS got_rows,
        MAX(CAST(r.rate AS {{ toorow_decimal_type(38, 9) }}))     AS got_rate,
        MAX(r.fx_gap_code)                      AS got_gap
    FROM probe p
    LEFT JOIN resolved r
      ON  r.connector      = p.connector
      AND r.project_id     = p.project_id
      AND r.base_currency  = 'USD'
      AND r.quote_currency = 'EUR'
      -- The caller's predicate, verbatim: half-open, so no day is claimed twice
      -- and none falls between two segments.
      AND p.on_date >= r.seg_from
      AND p.on_date <  r.seg_until
    GROUP BY p.case_name, p.expect_rate, p.expect_gap, p.expect_rows, p.meaning
),

-- (b) EVERY CASE, INCLUDING `got_rows` -- because a second matching segment is a
-- defect of its own: it would fan the staging model out and double every figure
-- of that day, which no assertion about the RATE would ever notice.
mismatches AS (
    SELECT
        case_name,
        expect_rate,
        got_rate,
        expect_gap,
        got_gap,
        expect_rows,
        got_rows,
        'FX_POSED_RESOLUTION_DISAGREES: ' || meaning
            || ' -- expected rows=' || CAST(expect_rows AS {{ dbt.type_string() }})
            || ' gap=' || COALESCE(expect_gap, '(none)')
            || ', got rows=' || CAST(got_rows AS {{ dbt.type_string() }})
            || ' gap=' || COALESCE(got_gap, '(none)')            AS failure_reason
    FROM matched
    WHERE got_rows <> expect_rows
       OR (got_rate IS NULL) <> (expect_rate IS NULL)
       OR ABS(COALESCE(got_rate, 0) - COALESCE(expect_rate, 0)) > 0.000001
       OR COALESCE(got_gap, '(none)') <> COALESCE(expect_gap, '(none)')
)

SELECT case_name, expect_rate, got_rate, expect_gap, got_gap,
       expect_rows, got_rows, failure_reason
FROM cardinality_guard
UNION ALL
SELECT case_name, expect_rate, got_rate, expect_gap, got_gap,
       expect_rows, got_rows, failure_reason
FROM mismatches
