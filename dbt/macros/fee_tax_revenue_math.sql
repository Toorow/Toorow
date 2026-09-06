{% macro fee_tax_vat_net_of_micros(gross_micros, rate) %}
{#- Epic 41 / Story 41.5 -- VAT deduction: the DERIVED NET TOTAL, not the component.

    net_total = gross / (1 + rate). This is the SAME NON-ADDITIVE SHAPE as 41.3's WHT
    gross-up (base / (1 - rate)) and it gets EXACTLY the same discipline, deliberately,
    so the two cannot drift into different rounding stories:

      * ONE rounding, on the DERIVED TOTAL. The caller obtains the component by EXACT
        INTEGER SUBTRACTION -- sales_tax_micros = gross - net -- so
        `net + tax == gross` holds TO THE MICRO, ALWAYS, and the waterfall identity is
        asserted at a tolerance of literally ZERO. Rounding the tax independently would
        make the total and the component two separately-rounded quotients that can
        disagree by a micro; the identity test would then fail, correctly, and someone
        would be tempted to "fix" it by loosening the tolerance. Do not go there: the
        identity is not an approximation to be tolerated, it is a construction.

      * THE OTHER DIRECTION IS AN ADD AND REUSES fee_tax_pct_of_micros UNMODIFIED. When
        the landed figure is already HT (landed_tax_posture = 'TAX_EXCLUSIVE'),
        sales_tax_micros = fee_tax_pct_of_micros(net, rate) -- one rounding, on the
        COMPONENT this time -- and gross = net + tax, exact by construction. THE
        ASYMMETRY IS DELIBERATE AND IS THE SAME PRINCIPLE BOTH TIMES: round the DERIVED
        quantity exactly once, then obtain the other side by exact integer arithmetic.
        Do NOT "harmonise" the two branches into two roundings.

    ==================== GUARDS, AND THE ONE THAT IS ABSENT ====================
    fee_tax_grossup_micros refuses `rate >= 1` because 1 - rate reaches zero and the
    division would be by zero. HERE THAT GUARD DOES NOT APPLY AND IS DELIBERATELY
    OMITTED: 1 + rate is >= 1 for every rate >= 0, so there is no division by zero to
    guard and NO UPPER BOUND ON THE RATE IS IMPOSED. A reviewer who "restores" the
    >= 1 guard by symmetry starts refusing legitimate high-rate jurisdictions for no
    arithmetic reason. Only `rate IS NULL` or `rate < 0` yield NULL, and the caller
    turns that into VAT_RATE_INVALID.

    NO MAGNITUDE GUARD EITHER, and for a reason that is also arithmetic rather than
    stylistic: the gross-up guard exists because base/(1-w) GROWS without bound as w
    approaches 1 and can exceed INT64. Dividing by (1 + rate) >= 1 can only SHRINK the
    magnitude, so the quotient of a valid BIGINT is always a valid BIGINT. There is
    nothing to overflow.

    ==================== WIDTHS ARE LOAD-BEARING ==============================
    DECIMAL(28,0) and DECIMAL(10,6) -- the SAME pair fee_tax_pct_of_micros and
    fee_tax_grossup_micros use. At DECIMAL(29,0) the derived precision leaves DuckDB's
    38-digit maximum and exactness is no longer provable. Do not widen either operand.
    The cross-adapter branch has the same single reason as the other money macros:
    DuckDB requires a parameterised DECIMAL(p,s) to stay in exact decimal arithmetic and
    BigQuery does not accept one inside a CAST, offering bare NUMERIC fixed at (38,9).

    ==================== HONEST NOTE ON EXACTNESS =============================
    DuckDB promotes DECIMAL / DECIMAL division to DOUBLE (verified for the gross-up
    macro, and this is the same construct), so THIS SINGLE DIVISION IS ONE OF THE TWO
    STEPS IN THE WHOLE ENGINE THAT IS NOT EXACT DECIMAL ARITHMETIC. The magnitudes this
    model handles are far below 2^53 and the immediately-following ROUND collapses the
    ~1e-16 relative residue; the gross-up macro's own measured note records that the
    residue only becomes visible above ~1e13 micros on a single row. What survives at
    EVERY magnitude is the identity net + tax == gross, because the component is an
    integer subtraction of the returned value rather than a second rounded quotient --
    only the last micro of the derived net is uncertain at extreme magnitudes.
    BigQuery's NUMERIC division stays exact at (38,9).
    ("Consider exact integer division to remove the engine's last non-exact step" is
    logged as a Story 41.9 retro item. It is NOT a mid-wave refactor.)

    ==================== THE ROUNDING DIVERGENCE, PINNED ======================
    SQL ROUND is half-away-from-zero; server/core/money.py uses Python's banker's
    rounding. At an exact half-micro the two differ by 1 micro. Fixture scenario R2
    (gross = 12 000 000 003 at rate 0.200000 -> 10 000 000 002.5 algebraically) lands on
    exactly such a half so the divergence is PINNED rather than discovered later. -#}
CASE
    WHEN {{ rate }} IS NULL OR {{ rate }} < 0 THEN NULL
    {%- if target.type == 'bigquery' %}
    ELSE
    CAST(ROUND(CAST({{ gross_micros }} AS NUMERIC) / (1 + CAST({{ rate }} AS NUMERIC))) AS BIGINT)
    {%- else %}
    ELSE
    CAST(ROUND(CAST({{ gross_micros }} AS DECIMAL(28,0)) / (1 + CAST({{ rate }} AS DECIMAL(10,6)))) AS BIGINT)
    {%- endif %}
END
{% endmacro %}
