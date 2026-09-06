{% macro fee_tax_pct_of_micros(base_micros, rate) %}
{#- Epic 41 / Story 41.3 -- percentage-of-base in EXACT integer micros.

    Cross-adapter for the same single reason as fee_tax_to_micros: a parameterised
    DECIMAL(p,s) is required by DuckDB to stay in exact decimal arithmetic and is
    NOT accepted inside a BigQuery CAST (BigQuery has bare NUMERIC, fixed at (38,9),
    which is exact for these magnitudes).

    WIDTHS ARE LOAD-BEARING, DO NOT WIDEN EITHER OPERAND -- and here is the ACTUAL
    reason (an earlier revision of this comment stated a false one; corrected after
    review verified it on DuckDB 1.5.4):
      DuckDB derives a DECIMAL product's type as (p1+p2, s1+s2). DECIMAL(28,0) *
      DECIMAL(10,6) -> (38,6), which is EXACTLY DuckDB's 38-digit maximum, so the
      result type is the natural one, no clamping happens, and the multiply is exact
      for every representable input.
      At DECIMAL(29,0) the natural precision is 39, ABOVE the maximum. DuckDB does
      NOT fall back to DOUBLE (verified: typeof(...) is still DECIMAL(38,6)) -- it
      CLAMPS the result type. The multiply is then no longer guaranteed representable
      and a large enough operand raises an out-of-range error at runtime instead of
      being provably in range. 28 is the widest base that is provably safe.

    `rate` is an exact decimal fraction (0.030000 = 3 %), NUMERIC(12,6) in Postgres.
    The CAST to DECIMAL(10,6) re-quantises to exactly six decimal places, half away
    from zero.

    Rounding policy: ROUND_HALF_UP, ONE rounding per component -- EXCEPT on the
    plan-coverage path, which is honestly TWO (§D.5 / finding F11): the caller may
    pass `rate * coverage_micros / 1000000`, whose division is DOUBLE and is then
    re-quantised to DECIMAL(10,6) by the CAST below (0.030000 x 0.333333 ->
    0.00999999 -> 0.010000), so the rate is rounded and then the component is. The
    common case is exempt: the store enforces SUM(split_weight) = 1.0 per
    (plan, connector, campaign_ref), so the caller's equality fast path passes `rate`
    untouched and that path is bit-exact with a single rounding. -#}
{%- if target.type == 'bigquery' -%}
CAST(ROUND(CAST({{ base_micros }} AS NUMERIC) * CAST({{ rate }} AS NUMERIC)) AS BIGINT)
{%- else -%}
CAST(ROUND(CAST({{ base_micros }} AS DECIMAL(28,0)) * CAST({{ rate }} AS DECIMAL(10,6))) AS BIGINT)
{%- endif -%}
{% endmacro %}


{% macro fee_tax_grossup_micros(base_micros, rate) %}
{#- Epic 41 / Story 41.3 -- WHT gross-up: the GROSSED TOTAL, not the component.

    Phase 4 is the one non-additive step of the cascade. The other phases ADD; a
    withholding tax RESCALES: the supplier must still receive `base` after a
    withholding of `w`, so the payer owes base / (1 - w).

    WHY THIS RETURNS THE TOTAL AND THE CALLER SUBTRACTS -- with the rationale CORRECTED
    after measurement, because the one written here first was wrong.
    An earlier revision claimed "ROUND(base * w/(1-w)) and ROUND(base/(1-w)) - base can
    differ by one micro". They CANNOT, as a matter of arithmetic: base is an INTEGER, so
    frac(base + x) == frac(x) and ROUND(base + x) == base + ROUND(x) for every x. The two
    formulas are mathematically IDENTICAL here. Fuzzing 2 000 (base, rate) pairs per
    magnitude against DuckDB found 0 divergences at 1e9 and 1e11 micros, 4/2000 at 1e13
    and 183/2000 at 1e15 -- and every one of those is FLOAT ERROR in the DECIMAL/DECIMAL
    division (see the exactness note below), not a genuine rounding difference.
    The choice is still the right one, for two reasons that survive:
      * SEMANTICS -- the number that is actually PAID is the grossed total, so that is
        the figure that must be a whole micro. The component is a derived presentation of
        it, not the other way round.
      * ONE FLOAT EXPRESSION, NOT TWO -- rounding the component would make the total and
        the component two independently-rounded quotients that can disagree at high
        magnitudes; subtracting keeps them a single consistent pair.
    base + component == grossed_total holds BY CONSTRUCTION as integer algebra, which is
    why no test asserts it (review finding F3 removed the one that did). And, stated
    plainly so nobody claims otherwise: NO test in this story can distinguish the two
    formulas at fixture magnitudes, BECAUSE THEY ARE EQUAL THERE. That is a property of
    the arithmetic, not a hole in the suite.

    FAIL-CLOSED GUARDS (E41-NFR02), inside the macro so a caller cannot forget them:
      * rate = 0            -> 1 - 0 = 1 -> grossed = base -> component exactly 0.
      * rate >= 1, rate < 0 -> NULL. The caller raises gap_wht_rate_invalid and the
        headline totals go NULL. The division is CASE-guarded, not merely flagged:
        a flag alone would still let the engine evaluate base/0.
      * rate NULL           -> NULL (a GROSS_UP rule with no rate is not executable).
      * MAGNITUDE (finding F10) -> NULL. `rate` up to 0.999999 is admitted by
        migration 119's CHECK, and at that rate a base above ~9.2e12 micros makes the
        quotient exceed INT64: DuckDB then raises
        `Conversion Error: Type DOUBLE with value 1e+19 ... out of range for INT64`,
        i.e. it FAILS THE BUILD instead of producing the typed gap E41-NFR02 requires.
        The guard below is the exact rearrangement of `base/(1-w) <= INT64_MAX`:
        base > (1 - w) * 9_223_372_036_000_000_000, written as
        (1000000 - w*1000000) * 9223372036000 so every operand stays inside
        DECIMAL(38,x) with no big-product overflow of its own. The caller turns the
        NULL into gap_wht_base_overflow.

    Cross-adapter branch: identical reason to fee_tax_pct_of_micros.

    HONEST NOTE ON EXACTNESS -- with the REAL bound, not a comfortable one. DuckDB
    promotes DECIMAL/DECIMAL division to DOUBLE (verified), so this single division is
    the one step of the whole engine that is not exact decimal arithmetic. Review
    fuzzed 2 500 (base, rate) pairs per magnitude against Python `Decimal`:
        1e11 micros -> 0/2500 divergences
        1e13 micros -> 1/2500
        1e14 micros -> 16/2500
        1e15 micros -> 137/2500
    So "the following ROUND collapses the residue" is TRUE ONLY BELOW ~1e13 micros
    (about 10 million currency units of base on ONE ladder row); above that a
    one-micro divergence from the exact rational is reachable. What survives at every
    magnitude is the identity base + component == grossed_total, because the component
    is an integer subtraction of the returned value -- so the waterfall never breaks,
    only the last micro of the grossed total is uncertain at extreme magnitudes.
    BigQuery's NUMERIC division stays exact at (38,9). -#}
CASE
    WHEN {{ rate }} IS NULL OR {{ rate }} >= 1 OR {{ rate }} < 0 THEN NULL
    {%- if target.type == 'bigquery' %}
    WHEN {{ base_micros }} > (1000000 - CAST({{ rate }} AS NUMERIC) * 1000000) * 9223372036000 THEN NULL
    ELSE
    CAST(ROUND(CAST({{ base_micros }} AS NUMERIC) / (1 - CAST({{ rate }} AS NUMERIC))) AS BIGINT)
    {%- else %}
    WHEN {{ base_micros }} > (1000000 - CAST({{ rate }} AS DECIMAL(10,6)) * 1000000) * 9223372036000 THEN NULL
    ELSE
    CAST(ROUND(CAST({{ base_micros }} AS DECIMAL(28,0)) / (1 - CAST({{ rate }} AS DECIMAL(10,6)))) AS BIGINT)
    {%- endif %}
END
{% endmacro %}


{% macro fee_tax_alloc_floor_micros(amount_micros, part_micros, total_micros) %}
{#- Epic 41 / Story 41.3 -- the floor of a pro-rata share, for the FLAT allocation.

    ADDED AFTER REVIEW (finding F8, orchestrator ruling). A FLAT amount applies ONCE
    per (project, date, currency) and is ALLOCATED across that day's ladder rows
    pro-rata by net media. Callers use the telescoping form -- allocation for a row is
    `floor_share(cumulative_through_this_row) - floor_share(cumulative_before_it)` --
    which is the largest-remainder discipline `mediaplan_store.compute_spread` already
    uses for budgets, expressed as a window instead of a Python loop.

    WHY THIS IS A THIRD MACRO when the story budgeted two: the ban list forbids an
    inline parameterised DECIMAL(p,s) in a model, and this allocation cannot be done
    in BIGINT -- `amount * part` reaches ~1e25 for realistic magnitudes and DuckDB
    raises `Out of Range Error: Overflow in multiplication of INT64` (verified). So
    the width has to live behind a target.type branch, i.e. in a macro. Recorded as a
    deviation rather than smuggled in.

    WIDTHS: DECIMAL(19,0) each, because a BIGINT needs at most 19 digits and 19+19=38
    is EXACTLY DuckDB's maximum -- so the product's natural type is used, nothing is
    clamped, and the multiply is exact for EVERY pair of BIGINTs. (The same reasoning
    as fee_tax_pct_of_micros' 28+10; see that macro for what clamping actually does.)

    EXACTNESS OF THE DAY TOTAL is guaranteed by CONSTRUCTION, not by the division:
    `part >= total` short-circuits to the amount itself, so the LAST row of a group
    telescopes to exactly `amount_micros` no matter how the intermediate DOUBLE
    division rounded. Float error can only move a single micro BETWEEN two adjacent
    rows; it can never change the sum. Verified over amounts 1, 7, 5e8, 1.23e11 and
    1e12 across a 7-row group with deliberately awkward weights.

    `total_micros = 0` returns NULL: the caller substitutes an ordinal allocation, so
    a day whose net media is entirely zero still spreads the flat fee exactly. -#}
CASE
    WHEN {{ total_micros }} IS NULL OR {{ total_micros }} <= 0 THEN NULL
    WHEN {{ part_micros }} >= {{ total_micros }} THEN CAST({{ amount_micros }} AS BIGINT)
    WHEN {{ part_micros }} <= 0 THEN 0
    ELSE
    {%- if target.type == 'bigquery' %}
    CAST(FLOOR(CAST({{ amount_micros }} AS NUMERIC) * CAST({{ part_micros }} AS NUMERIC)
               / CAST({{ total_micros }} AS NUMERIC)) AS BIGINT)
    {%- else %}
    CAST(FLOOR(CAST({{ amount_micros }} AS DECIMAL(19,0)) * CAST({{ part_micros }} AS DECIMAL(19,0))
               / CAST({{ total_micros }} AS DECIMAL(19,0))) AS BIGINT)
    {%- endif %}
END
{% endmacro %}


{% macro fee_tax_pct_of_micros_weighted(base_micros, rate, coverage_micros) %}
{#- Story 48.4 -- percentage of a PLAN-COVERAGE-WEIGHTED base, with ONE rounding.

    This replaces the caller-side expression `rate * coverage_micros / 1000000`, which
    fee_tax_pct_of_micros' own docstring already named as the epic's remaining
    exactness hole (finding F11): that division is DOUBLE, and its result was then
    re-quantised to DECIMAL(10,6) by the percentage macro -- so the plan-coverage path
    rounded the RATE and then rounded the COMPONENT. Two rounding boundaries, in binary
    floating point, on a number that reaches an invoice.

    AC6 requires "one declared rounding policy" and "one cross-adapter exact
    algorithm", so the weight is folded into the SAME product as the rate and the
    single division happens once, in exact decimal, immediately before the single
    ROUND.

    WIDTHS ARE LOAD-BEARING, exactly as in fee_tax_pct_of_micros, and derived the same
    way (DuckDB types a DECIMAL product as (p1+p2, s1+s2) and CLAMPS above 38 digits
    rather than falling back to DOUBLE):

      rate      DECIMAL(10,6)   as in fee_tax_pct_of_micros
      coverage  DECIMAL(7,0)    a weight in micros is at most 1 000 000: 7 digits
      rate * coverage        -> DECIMAL(17,6)   exact
      base      DECIMAL(19,0)   a BIGINT needs at most 19 digits, as in
                                fee_tax_alloc_floor_micros
      base * (rate*coverage) -> DECIMAL(36,6)   exact, and 2 digits below the maximum

    Do not widen `base` to DECIMAL(28,0) to match fee_tax_pct_of_micros: 28+17 = 45
    clamps, and a clamped product is no longer provably representable.

    BigQuery uses BIGNUMERIC rather than NUMERIC: NUMERIC is fixed at (38,9) and the
    rate x weight product carries 12 decimal places, so NUMERIC would round the weight
    before the component -- reintroducing the second boundary this macro removes. -#}
{%- if target.type == 'bigquery' -%}
CAST(ROUND(CAST({{ base_micros }} AS BIGNUMERIC)
           * CAST({{ rate }} AS BIGNUMERIC)
           * CAST({{ coverage_micros }} AS BIGNUMERIC)
           / CAST(1000000 AS BIGNUMERIC)) AS INT64)
{%- else -%}
CAST(ROUND(CAST({{ base_micros }} AS DECIMAL(19,0))
           * (CAST({{ rate }} AS DECIMAL(10,6)) * CAST({{ coverage_micros }} AS DECIMAL(7,0)))
           / CAST(1000000 AS DECIMAL(7,0))) AS BIGINT)
{%- endif -%}
{% endmacro %}


{% macro fee_tax_exact_ratio(numerator_micros, denominator_micros) %}
{#- Story 48.4 -- a unit-free ratio computed in exact decimal, not binary float.

    `revenue_micros / NULLIF(cost_micros, 0)` divides two BIGINTs. DuckDB returns
    DOUBLE and BigQuery returns FLOAT64, so the reported ROAS carried binary
    floating-point error whose low bits depend on evaluation order and on the adapter
    -- the two warehouses could print different digits for the same two integers.

    A ratio is not generally terminating, so "exact" here means DECIMAL arithmetic
    with ONE DECLARED SCALE rather than a value with no error: 9 decimal places, which
    is BigQuery NUMERIC's own scale, so both adapters quantise at the same place and
    agree digit for digit.

    Zero or NULL denominator yields NULL, never infinity -- the caller's
    RATIO_UNDEFINED_ZERO_COST gap says why. -#}
{%- if target.type == 'bigquery' -%}
CAST(SAFE_DIVIDE(CAST({{ numerator_micros }} AS BIGNUMERIC),
                 CAST(NULLIF({{ denominator_micros }}, 0) AS BIGNUMERIC)) AS NUMERIC)
{%- else -%}
CAST(CAST({{ numerator_micros }} AS DECIMAL(19,0))
     / CAST(NULLIF({{ denominator_micros }}, 0) AS DECIMAL(19,0)) AS DECIMAL(29,9))
{%- endif -%}
{% endmacro %}
{#- ---------------------------------------------------------------------------
    Story 61.4. `fee_tax_from_micros` lives HERE and not beside its forward twin
    in `fee_tax_to_micros.sql`, and that is a measured choice rather than a
    filing preference: `tests/conformance/test_epic41_verification_overlay_additive.py`
    holds the exact-money macros byte-identical to HEAD, and Story 48.4 opened
    exactly ONE of them to appends -- this file -- on the argument that a NEW
    macro appended after the existing ones changes no existing call site. That
    argument is the same one here, so the macro goes where the door already is
    instead of asking for a second one. This file is also where the other
    unit-crossing helper lives (`fee_tax_exact_ratio`), so it is not an exile.
--------------------------------------------------------------------------- -#}

{% macro fee_tax_from_micros(micros_col) %}
{#- The inverse, and it lives here so the two cannot drift apart -- Story 61.4.

    Epic 41's marts stay in micros end to end and never come back, so this had no
    caller. The plan-pacing marts DO need it: `budget`, `allocated_amount` and
    `actual_amount` are read by four production callers (the pacing route, the MCP
    card `mediaplan_pacing`, `mediaplan_alerts` and the scheduler) as display
    decimals, and renaming them to `*_micros` would move the repair's cost onto
    four readers for no gain. So the composition happens in exact integer micros
    and the display value is derived from it ONCE, here.

    It is NOT `micros / 1000000`: dividing a BIGINT by an integer yields DOUBLE in
    DuckDB and FLOAT64 in BigQuery, which is the exact float this macro's twin was
    written to remove. Multiplying by an exact DECIMAL(10,6) stays in decimal
    arithmetic. The widths are chosen the same way `fee_tax_pct_of_micros`
    explains: DuckDB derives a product's type as (p1+p2, s1+s2), so
    DECIMAL(26,0) x DECIMAL(10,6) -> (36,6), inside the 38-digit maximum with room
    to spare, and 26 digits of micros is 10^20 units -- far beyond any budget.

    Exact for every micro value: 0.000001 is representable in DECIMAL(10,6). -#}
{%- if target.type == 'bigquery' -%}
CAST(CAST({{ micros_col }} AS NUMERIC) * CAST(0.000001 AS NUMERIC) AS NUMERIC)
{%- else -%}
(CAST({{ micros_col }} AS DECIMAL(26,0)) * CAST(0.000001 AS DECIMAL(10,6)))
{%- endif -%}
{% endmacro %}


{% macro fee_tax_run_rate_micros(actual_micros, days_total, days_elapsed) %}
{#- AI-268 -- the run-rate extrapolation, and the reason it is a macro at all.

    `plan_pacing_by_line.sql` wrote this arithmetic INLINE, with parametrised
    `DECIMAL(p,s)` casts and NO `target.type` branch. DECIMAL(p,s) is DuckDB's
    spelling; BigQuery has NUMERIC and BIGNUMERIC and takes no precision
    arguments, so the model did not compile there at all.

    It had no visible effect, and that is exactly what made it worth repairing
    rather than noting: the family is already DuckDB-only through an earlier
    CAST, so nothing was WRONG on screen -- the falseness sat in the load,
    waiting for the day another reader took it. Story 60.3 made the rule
    explicit by requiring a pattern to compile in BOTH dialects, and
    `fee_tax_exact_ratio` above is the shape this follows.

    ONE ROUNDING, at the end. The multiplication is exact on integers; only the
    division needs a scale, and it takes the same 9 places `fee_tax_exact_ratio`
    uses -- BigQuery NUMERIC's own scale -- so both adapters quantise at the same
    place and agree digit for digit.

    A zero or NULL elapsed count yields NULL, never a division by zero: a line
    that has not started has no run rate, which is not the same as a run rate of
    zero. -#}
{%- if target.type == 'bigquery' -%}
CAST(ROUND(CAST(SAFE_DIVIDE(
        CAST({{ actual_micros }} AS BIGNUMERIC) * CAST({{ days_total }} AS BIGNUMERIC),
        CAST(NULLIF({{ days_elapsed }}, 0) AS BIGNUMERIC)) AS NUMERIC)) AS INT64)
{%- else -%}
CAST(ROUND(CAST(
        CAST({{ actual_micros }} AS DECIMAL(19,0))
        * CAST({{ days_total }} AS DECIMAL(9,0))
        / CAST(NULLIF({{ days_elapsed }}, 0) AS DECIMAL(9,0))
    AS DECIMAL(29,9))) AS BIGINT)
{%- endif -%}
{% endmacro %}
