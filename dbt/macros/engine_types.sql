{#- ===================================================================== -#}
{#- ONE SPELLING PER TYPE, TWO ENGINES -- the rule this file exists for.   -#}
{#-                                                                        -#}
{#- toorow builds the same project on DuckDB locally and on BigQuery in    -#}
{#- production. `DOUBLE`, `VARCHAR` and any parameterised `DECIMAL(p, s)`  -#}
{#- are DuckDB spellings that BigQuery refuses outright -- the last one    -#}
{#- with *Parameterized types are not allowed in CAST expressions*. A      -#}
{#- relation or test that carries one of them compiles green locally and   -#}
{#- cannot run in production: `execution-substrate.md` "Incomplete if" 23. -#}
{#-                                                                        -#}
{#- The convention is NOT new. `toorow_exact_money_type()` (Story 67.13,   -#}
{#- `fx_convert_at_read.sql`) already branches on `target.type` for the    -#}
{#- money decimal, and the seeds are dual the same way. These macros       -#}
{#- EXTEND that one convention to the other two types so the repo has one  -#}
{#- rule and not three; the money macro now delegates its width here so    -#}
{#- the branch itself is written once.                                     -#}
{#-                                                                        -#}
{#- Use them everywhere a CAST or a DDL names a type. Never write a bare   -#}
{#- `DOUBLE`, `VARCHAR` or `DECIMAL(...)` in `dbt/models/**` or            -#}
{#- `dbt/tests/**` again: `server/tests/conformance/                       -#}
{#- test_dbt_speaks_both_engines.py` refuses a new one, and the BigQuery   -#}
{#- half of that gate dry-runs the compiled SQL at 0 bytes billed.         -#}
{#-                                                                        -#}
{#- dbt's own `dbt.type_float()` / `type_string()` are NOT used here for   -#}
{#- one reason and one only: they cannot express the exact decimal, so     -#}
{#- half the rule would live in dbt and half in toorow. `dbt.type_numeric` -#}
{#- resolves to `numeric(28, 6)` and drops three digits of an FX rate.     -#}
{#- ===================================================================== -#}


{% macro toorow_float_type() %}
{#- Approximate binary float. `FLOAT64` on BigQuery, `DOUBLE` on DuckDB --
    the same IEEE-754 double on both, so nothing changes but the word.

    This is the right type for a COUNT-like measure (sessions, impressions,
    clicks) summed into `fact_daily_kpi.value`. It is the WRONG type for money:
    a binary float cannot hold a published rate or an amount exactly, which is
    the defect Story 48.3 removed from `fx_convert_at_read`. Money is
    `toorow_exact_money_type()`. -#}
{%- if target.type == 'bigquery' -%}
FLOAT64
{%- else -%}
DOUBLE
{%- endif -%}
{% endmacro %}


{% macro toorow_string_type() %}
{#- Variable-length text. `STRING` on BigQuery, `VARCHAR` on DuckDB. Neither
    engine takes the other's word, and BigQuery has no length-parameterised
    form to fall back on. Both are unbounded, so a CAST through this macro
    truncates nothing on either engine. -#}
{%- if target.type == 'bigquery' -%}
STRING
{%- else -%}
VARCHAR
{%- endif -%}
{% endmacro %}


{% macro toorow_decimal_type(precision, scale) %}
{#- An EXACT decimal of the given width.

    DuckDB spells it `DECIMAL(p, s)`. BigQuery has no parameterised decimal in
    a CAST at all -- it has two fixed ones, and the branch below picks the
    narrowest that HOLDS the requested width rather than approximating it:

      * `NUMERIC`    is exactly DECIMAL(38, 9) -- 29 integer digits, 9
                     fractional. Chosen when scale <= 9 and integer digits <= 29.
      * `BIGNUMERIC` is DECIMAL(76, 38). Chosen when the width does not fit
                     NUMERIC but fits this.

    Anything wider is a compile error rather than a silent narrowing: a
    truncated money amount that still returns a row is the failure mode this
    whole file exists to prevent.

    Widening on BigQuery is deliberate and safe -- every value of the requested
    DECIMAL(p, s) is representable in the chosen type, exactly. It is not
    symmetric with DuckDB's own bare `NUMERIC`, which defaults to DECIMAL(18, 3)
    and WOULD truncate; that asymmetry is why this cannot be one word for both. -#}
{%- set precision = precision | int -%}
{%- set scale = scale | int -%}
{%- if scale < 0 or precision <= 0 or scale > precision -%}
  {{ exceptions.raise_compiler_error("toorow_decimal_type: nonsensical width DECIMAL(" ~ precision ~ ", " ~ scale ~ ")") }}
{%- endif -%}
{%- if target.type == 'bigquery' -%}
  {%- if scale <= 9 and (precision - scale) <= 29 -%}
NUMERIC
  {%- elif scale <= 38 and (precision - scale) <= 38 -%}
BIGNUMERIC
  {%- else -%}
  {{ exceptions.raise_compiler_error("toorow_decimal_type: DECIMAL(" ~ precision ~ ", " ~ scale ~ ") has no exact BigQuery type; widen the design, not the type") }}
  {%- endif -%}
{%- else -%}
DECIMAL({{ precision }}, {{ scale }})
{%- endif -%}
{% endmacro %}


{% macro toorow_star_except(alias, columns) %}
{#- Every column of `alias` EXCEPT the named ones -- the idiom a staging uses to
    replace one landed column with a normalised version of itself without
    re-listing thirty others.

    NOT A TYPE, and it lives here anyway: this file is where the ONE spelling
    per engine is decided, and a second dialect file would be a second place to
    look. DuckDB spells the clause `EXCLUDE (...)`; BigQuery spells it
    `EXCEPT (...)` -- the same feature, two words, and each engine refuses the
    other's outright.

    Five connector stagings spelled the DuckDB word and were frozen in the
    2026-08-31 dry-run allowlist for it (adobe-analytics, amazon-dsp, brevo,
    sa360, x-ads); the first real per-project run is what made them a production
    outage rather than a note. Call it as:

        SELECT
            {{ toorow_star_except('raw', ['metric']) }},
            COALESCE(nm.canonical_metric, raw.metric) AS metric
        FROM raw ...

    `columns` is a list, so dropping a second column is an edit to the list and
    not to the SQL around it. -#}
{%- if columns is string -%}
  {%- set columns = [columns] -%}
{%- endif -%}
{%- if columns | length == 0 -%}
  {{ exceptions.raise_compiler_error("toorow_star_except: no column named; write `" ~ alias ~ ".*`") }}
{%- endif -%}
{%- if target.type == 'bigquery' -%}
{{ alias }}.* EXCEPT ({{ columns | join(', ') }})
{%- else -%}
{{ alias }}.* EXCLUDE ({{ columns | join(', ') }})
{%- endif -%}
{% endmacro %}


{% macro toorow_try_cast(expression, type) %}
{#- A CAST that yields NULL instead of failing.

    DuckDB spells it `TRY_CAST(x AS T)`; BigQuery spells it `SAFE_CAST(x AS T)`
    and has no `TRY_CAST` at all. `type` is passed through as written, so it
    must itself come from a macro above -- `toorow_try_cast(x,
    toorow_float_type())` and never `AS FLOAT`, which is a third spelling
    neither engine shares (DuckDB aliases it to DOUBLE, BigQuery has only
    FLOAT64).

    Found 2026-08-31 in `anomalies_daily.sql`, by the widened offline scan and
    not by the dry run: the node reads a source the dry-run warehouse does not
    have, so BigQuery answered *Not found: Table* and never got as far as the
    function. A lexical scan has no such blind spot, which is the argument for
    keeping both halves. -#}
{%- if target.type == 'bigquery' -%}
SAFE_CAST({{ expression }} AS {{ type }})
{%- else -%}
TRY_CAST({{ expression }} AS {{ type }})
{%- endif -%}
{% endmacro %}


{% macro toorow_bool_or(expression) %}
{#- TRUE when the expression is TRUE on at least one row of the group.

    `BOOL_OR` is Postgres's and DuckDB's; BigQuery has neither it nor
    `BOOL_AND`, and answers *Function not found: BOOL_OR*. Its own words for the
    pair are `LOGICAL_OR` / `LOGICAL_AND`, which DuckDB in turn does not have.

    THE COMMENT THIS REPLACES SAID THE OPPOSITE -- "BOOL_OR is portable (DuckDB
    + BigQuery)", in `plan_pacing_by_line.sql` -- and production disproved it on
    2026-08-31: the model died with *Function not found: BOOL_OR at [122:9]* on
    BOTH per-project builds. Six call sites over three pacing marts carried it.
    Nothing had caught it: the lexical scan did not know the name, and the dry
    run never planned these models because they read `mirror.*`, which is not in
    that warehouse (an `absent` node is a blind spot, not a pass).

    SO THIS IS NOT A DIALECT BRANCH, and deliberately: `MAX(CASE ...)` is one
    spelling BOTH engines accept, so there is nothing to branch on. Writing it
    through a macro rather than by hand is about having ONE spelling site for a
    construct that reads badly inline, not about the engines disagreeing.

    NULL TRAVELS AS `BOOL_OR` MADE IT TRAVEL. The CASE yields 1 for TRUE, 0 for
    FALSE and NULL for NULL; `MAX` skips NULLs, so a group of only NULLs gives
    NULL and `= 1` gives NULL -- which is exactly what `BOOL_OR` answers there.
    A plain `ELSE 0` would have turned that NULL into FALSE, which is a
    different claim. -#}
(MAX(CASE WHEN ({{ expression }}) THEN 1 WHEN NOT ({{ expression }}) THEN 0 END) = 1)
{% endmacro %}


{% macro toorow_bool_and(expression) %}
{#- TRUE when the expression is TRUE on every row of the group.

    `BOOL_AND`'s twin, same story and same NULL rule as `toorow_bool_or` above:
    `MIN` skips NULLs, so an all-NULL group answers NULL rather than TRUE. -#}
(MIN(CASE WHEN ({{ expression }}) THEN 1 WHEN NOT ({{ expression }}) THEN 0 END) = 1)
{% endmacro %}
