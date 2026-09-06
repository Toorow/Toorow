{% macro fee_tax_cpm_micros(measured_impressions, cpm_micros) %}
{#- Epic 41 / Story 41.4 -- CPM over a measured-impression count, in EXACT integer micros.

    verification_cost_micros = ROUND(measured_impressions * cpm_micros / 1000)

    `cpm_micros` is "cost per 1 000 base units" (migration 119's column comment / epic
    C.2), in integer micros of the rule's own `currency`. `measured_impressions` is an
    integer count. Because CPM is LINEAR in impressions, the per-campaign formula IS the
    allocation-by-measured-impressions: the day total is the exact integer SUM of the
    campaign figures, there is no separately-computed day total for it to disagree with,
    and there is NO ALLOCATION RESIDUE to dump on an arbitrary campaign.

    Cross-adapter for the same single reason as fee_tax_to_micros / fee_tax_pct_of_micros:
    DuckDB needs an explicitly-parameterised DECIMAL(p,s) to stay in exact decimal
    arithmetic, and BigQuery does not accept a parameterised DECIMAL inside CAST at all
    (it offers bare NUMERIC, fixed at (38,9), which is exact for these magnitudes). That
    is the whole reason for the branch; it is not defensive.

    ================ NO DIVISION -- /1000 IS AN EXACT DECIMAL MULTIPLY ==========
    DuckDB promotes DECIMAL/DECIMAL division to DOUBLE (the honest note at
    fee_tax_money_math.sql lines 90-104). fee_tax_grossup_micros has no choice about
    that; THIS component does, so it takes the exact path: `cpm_micros / 1000` is
    expressed as `cpm_micros * 0.001` with both operands exact decimals. Do not
    "simplify" it back to `/ 1000` -- that silently leaves exact arithmetic.

    ================ WIDTHS ARE LOAD-BEARING -- DO NOT WIDEN EITHER OPERAND =====
    DuckDB derives a DECIMAL product's type as (p1+p2, s1+s2), and its maximum precision
    is 38.
      * DECIMAL(10,0) * DECIMAL(4,3) -> (14,3), exact. `cpm_micros` is capped below 1e10
        by the guard, so it is representable in DECIMAL(10,0), and the product
        cpm_micros/1000 has at most 7 integral digits and exactly 3 decimals. The
        re-quantisation to DECIMAL(10,3) is therefore LOSSLESS -- it is a re-typing, not
        a rounding -- and it is what keeps the second multiply inside the ceiling.
      * DECIMAL(28,0) * DECIMAL(10,3) -> (38,3), EXACTLY DuckDB's 38-digit maximum, so
        the result type is the natural one, nothing is clamped, and the multiply is exact
        for every representable input.
      * At DECIMAL(29,0) or DECIMAL(11,3) the natural precision is 39, ABOVE the maximum.
        DuckDB does NOT fall back to DOUBLE (verified for the sibling macro on DuckDB
        1.5.4: typeof(...) is still a DECIMAL) -- it CLAMPS the result type, the multiply
        is then no longer guaranteed representable, and a large enough operand raises an
        out-of-range error at runtime instead of being provably in range. 28 is the widest
        base that is provably safe.
        (The 41.4 story file states that widening makes DuckDB "silently fall back to
        DOUBLE with no error". That claim is WRONG -- it was measured and corrected in
        fee_tax_money_math.sql during 41.3's review. The conclusion "do not widen" is
        identical either way; the reason recorded here is the true one.)

    ================ FAIL-CLOSED GUARD, INSIDE THE MACRO (E41-NFR02) ===========
    The fee_tax_grossup_micros pattern: the guard lives HERE so a caller cannot forget it
    AND so the cast is never evaluated out of range.
      * cpm_micros IS NULL  -> NULL. A CPM rule with no rate is not executable.
      * cpm_micros < 0      -> NULL. A negative CPM is not a price.
      * cpm_micros >= 1e10  -> NULL. That is exactly the DECIMAL(10,0) capacity: above it
        the CAST would RAISE rather than return a number, i.e. a red build where
        E41-NFR02 requires a typed gap. The caller raises
        VERIFICATION_CPM_OUT_OF_RANGE and nulls the component.
    Note that measured_impressions is NOT guarded: it is a read of a real fact and is
    always populated (fact_daily_kpi's HAVING ... IS NOT NULL, AD-9). A measured ZERO is
    a real zero and yields exactly 0 -- the only legitimate 0 this engine emits.

    ================ ROUNDING: ROUND_HALF_UP, ONE BOUNDARY PER COMPONENT =======
    Both DuckDB's and BigQuery's ROUND() round half AWAY FROM ZERO natively, and the
    boundary is applied ONCE per (row x rule) component -- never ROUND(SUM(...)), which
    would put the single boundary at the wrong grain.

    DOCUMENTED DIVERGENCE (do not "fix" it): server/core/money.py uses Python round(),
    i.e. banker's rounding (half to even), so at an exact half-micro the SQL and the
    Python adapter can differ by 1 micro. The SQL side is the authority for this engine
    (arbitration B2), exactly as fee_tax_to_micros lines 26-30 established. Fixture row
    `half_up_even` lands on an exact half whose integer part is EVEN --
    999997 * 2500500 / 1000 = 2 500 492 498.5 -> 2 500 492 499 here, 2 500 492 498 under
    banker's rounding -- so the two policies give DIFFERENT answers and
    test_epic41_verification_round_half_up.sql discriminates between them instead of
    assuming. -#}
CASE
    WHEN {{ cpm_micros }} IS NULL
      OR {{ cpm_micros }} < 0
      OR {{ cpm_micros }} >= 10000000000 THEN NULL
    {%- if target.type == 'bigquery' %}
    ELSE
    CAST(ROUND(CAST({{ measured_impressions }} AS NUMERIC)
             * (CAST({{ cpm_micros }} AS NUMERIC) / 1000)) AS BIGINT)
    {%- else %}
    ELSE
    CAST(ROUND(CAST({{ measured_impressions }} AS DECIMAL(28,0))
             * CAST(CAST({{ cpm_micros }} AS DECIMAL(10,0))
                  * CAST(0.001 AS DECIMAL(4,3)) AS DECIMAL(10,3))) AS BIGINT)
    {%- endif %}
END
{% endmacro %}
