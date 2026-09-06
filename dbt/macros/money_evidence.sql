{#-
    The warehouse half of the Story 48.3 semantic evidence contract.

    `fact_daily_kpi` used to carry, for a monetary row, exactly four FX columns
    (`fx_rate`, `fx_as_of_date`, `fx_source`, `fx_tier`) and one `value` that was
    already converted. That is not enough for a reader to reproduce the meaning:

      * the NATIVE amount and its currency were gone, so "conversion overwrites
        the native amount" was true of the mart even though staging preserved
        `cost_source_value` one model upstream;
      * there was no way to tell an unconvertible row from a converted one --
        `COALESCE(fx_rate, 1.0)` made them identical;
      * nothing named the policy or rate-set version the conversion used, so a
        historical figure could not be re-derived after a policy change.

    These two macros emit the same column list on every UNION branch of the fact,
    which is the only way a 1600-line UNION model keeps a stable schema. A
    non-monetary branch calls `money_evidence_absent()`; a monetary one calls
    `money_evidence_present()`.
-#}

{% macro money_evidence_columns() %}
{#- The column names, in order. One definition, so the two emitters below cannot
    drift apart and produce a UNION with mismatched columns. -#}
{{ return([
    'native_value',
    'native_currency',
    'native_unit',
    'money_gap_code',
    'fx_rate',
    'fx_as_of_date',
    'fx_source',
    'fx_tier',
    'fx_method',
]) }}
{% endmacro %}


{% macro money_evidence_absent() %}
{#- A non-monetary series. Every evidence column is NULL and `money_gap_code` is
    NULL too: this row has no money to convert, which is not a gap. -#}
{#- CROSS-DIALECT CASTS, and they are not cosmetic (2026-08-24). `::` is
    Postgres/DuckDB syntax and `VARCHAR` is a DuckDB type name: BigQuery answers
    `Syntax error: Expected end of input but got ":"` and `Type not found:
    VARCHAR`. This macro is emitted by `fact_daily_kpi`'s empty branch -- the one
    a project whose sources are absent takes -- so the model could not compile at
    all on the engine production runs, while building green locally. Proven with
    a BigQuery dry run, which costs nothing and reads no byte. The 60.3 dialect
    obligation, applied where it had not been. -#}
    CAST(NULL AS {{ dbt.type_numeric() }}) AS native_value,
    CAST(NULL AS {{ dbt.type_string() }})  AS native_currency,
    CAST(NULL AS {{ dbt.type_string() }})  AS native_unit,
    CAST(NULL AS {{ dbt.type_string() }})  AS money_gap_code,
    CAST(NULL AS {{ dbt.type_numeric() }}) AS fx_rate,
    CAST(NULL AS DATE)                     AS fx_as_of_date,
    CAST(NULL AS {{ dbt.type_string() }})  AS fx_source,
    CAST(NULL AS {{ dbt.type_string() }})  AS fx_tier,
    CAST(NULL AS {{ dbt.type_string() }})  AS fx_method,
{% endmacro %}


{% macro money_evidence_present(native_amount_col, native_currency_col, native_unit='decimal') %}
{#- A monetary series.

    `native_value` is the SOURCE-currency amount, aggregated with the same SUM as
    the converted one, so the two are comparable row for row. It is emitted even
    when the conversion failed -- that is the point: the native evidence survives
    the gap, and a reporting-currency change re-derives from it without touching
    a source fact.

    `MIN(...)` on the currency rather than `MAX(...)`: it is deterministic, and a
    grain that somehow carried two source currencies is caught by the
    `fact_daily_kpi_single_native_currency` test rather than silently picking one.
-#}
    SUM(CAST({{ native_amount_col }} AS {{ toorow_exact_money_type() }})) AS native_value,
    MIN({{ native_currency_col }})                       AS native_currency,
    '{{ native_unit }}'                                  AS native_unit,
    -- Aggregate-safe, and it names WHICH thing is missing. The codes match
    -- server/core/money_derivation.py and server/core/fx_rate_sets.py exactly, so
    -- a warehouse reader and an application reader do not have two vocabularies
    -- for the same gap.
    --
    -- `fx_gap_code` FIRST, and that ordering is the point (Story 67.13). Before
    -- the FX cutover an unconvertible row could only say `fx_rate_unavailable` --
    -- "no rate was found" -- which is the honest word for an ABSENCE and a false
    -- one for a REFUSAL. A posed rate whose condition this grain cannot answer,
    -- and two equally specific posed rates that both match, are governance
    -- defects somebody has to go and repair; reporting them as a missing rate
    -- sends that person to look for a rate that is already there. Bullet 10 of
    -- docs/product-architecture/capabilities/currency-fx.md requires
    -- the conflict to be NAMED, and this is where a reader of the mart reads it.
    --
    -- Every caller of this macro is one of the thirteen staging models that
    -- carry `fx_gap_code` (measured: the callers of `money_evidence_present` and
    -- the models joining `ref('fx_rates')` are the same thirteen), so the column
    -- always resolves. A non-monetary branch calls `money_evidence_absent()` and
    -- never reaches this line.
    CASE WHEN MIN({{ native_currency_col }}) IS NULL THEN 'native_currency_missing'
         WHEN MAX(fx_gap_code) IS NOT NULL THEN MAX(fx_gap_code)
         WHEN MAX(fx_rate) IS NULL THEN 'fx_rate_unavailable'
         ELSE NULL END                                   AS money_gap_code,
    MAX(CAST(fx_rate AS {{ toorow_exact_money_type() }}))                 AS fx_rate,
    MAX(fx_as_of_date)                                   AS fx_as_of_date,
    MAX(fx_source)                                       AS fx_source,
    MAX(fx_tier)                                         AS fx_tier,
    -- CARRIED, never minted. This line used to read
    --     CASE WHEN MAX(fx_rate) IS NULL THEN NULL ELSE 'direct' END
    -- which stamped `direct` -- the word reserved for an OBSERVED quotation -- on
    -- a rate a human wrote into dbt/seeds/fx_rates.csv by hand. The number was
    -- disclosed (`fx_source='seed'`) under a method label that was not true of it.
    -- The amendment of 2026-08-17 to docs/product-architecture/alignment-register.md
    -- ("a fixed FX value is a first-class method, not a stopgap") settles it: a rate
    -- somebody POSED carries `fixed`, and it carries it from where it was posed --
    -- the seed's own `fx_method` column, propagated by every staging model beside
    -- `fx_source` / `fx_tier`. Nothing here derives a method, so nothing here can
    -- claim one the row does not have.
    --
    -- `fx_tier` is a different question and keeps its own word: the tier says which
    -- RATE TABLE served the row (fixed | historical, core.fx_helper), the method
    -- says how the number was OBTAINED (fixed | direct | triangulated |
    -- carry_forward | identity, core.fx_rate_sets).
    MAX(fx_method)                                       AS fx_method,
{% endmacro %}
