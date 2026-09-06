{#-
  A PROJECT HAS THE CONNECTORS IT HAS, AND THE FACT MUST BE BUILDABLE ANYWAY.

  `fact_daily_kpi` UNIONs fifty-two staging models, one per connector report
  profile, with no condition of any kind. That is only satisfiable by a project
  that uses EVERY connector -- which no real project does. Measured 2026-08-18 on
  the one production project that carries data (Sebastien Kardinal, YouTube only,
  34 640 raw rows, 547 videos): the nightly's own command,
  `dbt build --vars '{project, raw_schema}'`, returns **53 errors**, every one of
  them `Catalog Error: Table with name raw_<connector>_daily does not exist!`.
  So the transverse fact has never been built for that project, every card that
  reads it answers `warehouse_not_ready`, and the connector's own mart
  (`fact_youtube_daily`, which depends on YouTube alone) builds perfectly.

  `toorow_model_present` is the condition that was missing. Guarding a UNION
  branch with it drops the branch when its staging model is not in the warehouse,
  instead of failing the whole fact.

  AT PARSE TIME IT ANSWERS TRUE, ALWAYS, and that is not a shortcut: `ref()` is
  what builds the DAG, so the dependency must be declared whatever the answer
  will be at run time. Only when `execute` is true -- when dbt is actually
  running the model and the upstream relations either exist or do not -- does the
  catalogue get asked.

  WHAT IT DOES NOT DO. It does not make an ABSENT SOURCE survive: a staging model
  whose `raw_*` table is missing still errors, and dbt then SKIPS everything
  downstream, guard included. The two go together -- the runner excludes the
  staging models whose source the project does not have, and this macro drops
  their branches from the fact.
-#}
{% macro toorow_model_present(model_name) %}
    {%- if not execute -%}
        {{ return(True) }}
    {%- endif -%}
    {%- set relation = ref(model_name) -%}
    {%- set found = adapter.get_relation(
        database=relation.database,
        schema=relation.schema,
        identifier=relation.identifier,
    ) -%}
    {{ return(found is not none) }}
{% endmacro %}


{#-
  The same question asked of a declared SOURCE rather than of a model.

  `fact_daily_kpi_all_pulls` reads the raw landing zone directly -- it is the
  un-superseded twin of the fact -- so its branches are conditioned on the raw
  table, not on a staging view.
-#}
{% macro toorow_source_present(source_name, table_name) %}
    {%- if not execute -%}
        {{ return(True) }}
    {%- endif -%}
    {%- set relation = source(source_name, table_name) -%}
    {%- set found = adapter.get_relation(
        database=relation.database,
        schema=relation.schema,
        identifier=relation.identifier,
    ) -%}
    {{ return(found is not none) }}
{% endmacro %}


{#-
  THE SAME QUESTION ASKED OF SEVERAL TABLES AT ONCE, and it answers with the
  NAMES rather than with a boolean.

  A model that reads five mirror relations has to say WHICH of them is not there:
  "the mirror is incomplete" sends a repairer to look at all twenty-eight, and
  the whole point of the guard is that the build stops accusing the warehouse of
  being broken when a synchronisation simply has not happened.

  AT PARSE TIME IT ANSWERS `[]`, ALWAYS -- the same discipline as its two
  neighbours, and for the same reason: the real branch must be the one dbt parses
  so that every `source()` inside it is registered in the DAG. `source()` is
  called on every table even when the catalogue is not asked, so the dependency
  is declared from here too.
-#}
{% macro toorow_absent_sources(source_name, table_names) %}
    {%- set missing = [] -%}
    {%- for table_name in table_names -%}
        {%- set relation = source(source_name, table_name) -%}
        {%- if execute -%}
            {%- set found = adapter.get_relation(
                database=relation.database,
                schema=relation.schema,
                identifier=relation.identifier,
            ) -%}
            {%- if found is none -%}
                {%- do missing.append(table_name) -%}
            {%- endif -%}
        {%- endif -%}
    {%- endfor -%}
    {{ return(missing) }}
{% endmacro %}


{#-
  A PORTABLE EMPTY RELATION WITH DECLARED COLUMNS, in one place.

  Four models wrote this branch by hand and two of them wrote it WRONG in the
  same way: `SELECT CAST(NULL AS ...) ... WHERE 1 = 0` with no FROM. DuckDB
  accepts it, BigQuery answers *Query without FROM clause cannot have a WHERE
  clause*, so the branch built green locally and could not compile on the engine
  production runs -- and it is the ONLY branch a production build reaches, since
  it is the branch of a warehouse whose source is absent. Repaired once here
  rather than four times in four models.

  `columns` is a list of `[name, type]` pairs; the type is one of the eight words
  below and never an engine's spelling of it, because that spelling is exactly
  what does not travel (`VARCHAR` does not exist in BigQuery, `::` is not its
  syntax). `DATE` and `BOOLEAN` have no dbt macro and need none: both engines
  spell them that way.
-#}
{% macro toorow_empty_projection(columns) %}
    {%- set types = {
        'string': dbt.type_string(),
        'timestamp': dbt.type_timestamp(),
        'float': dbt.type_float(),
        'numeric': dbt.type_numeric(),
        'bigint': dbt.type_bigint(),
        'int': dbt.type_int(),
        'boolean': 'BOOLEAN',
        'date': 'DATE',
    } %}
SELECT
    {%- for name, type_name in columns %}
    {%- if type_name not in types %}
    {{ exceptions.raise_compiler_error("toorow_empty_projection: unknown type '" ~ type_name ~ "' for column '" ~ name ~ "'") }}
    {%- endif %}
    CAST(NULL AS {{ types[type_name] }}) AS {{ name }}{% if not loop.last %},{% endif %}
    {%- endfor %}
FROM (SELECT 1) AS _empty
WHERE FALSE
{% endmacro %}


{#-
  THE RELATION, OR AN EMPTY ONE OF THE SAME SHAPE -- for a source that is JOINED
  rather than selected from.

  Nine connector staging models `LEFT JOIN mirror.fx_source_currency_bindings` to
  let a Project override the source currency of one field. When the mirror is not
  in this warehouse -- which is every production build today, `mirror_sync`
  defers its BigQuery writes -- that join failed the model, so dbt skipped
  `fact_daily_kpi` behind it and the Project got no mart at all. Measured
  2026-08-24 by rebuilding against a warehouse whose `mirror` schema was dropped:
  ten staging models, one error each, `Catalog Error ... schema "mirror" does not
  exist`.

  Standing an EMPTY relation in its place is the honest reading and not a
  fallback: a LEFT JOIN that matches nothing is what "this Project declared no
  override" already means, so the COALESCE beside it falls back to the source's
  own currency exactly as it does for a Project that declared none. Guarding the
  JOIN itself would leave `fx_res.resolved_source_currency` unbound in the
  COALESCE; giving the alias a real, empty relation keeps one SQL shape.

  `columns` must carry every column the surrounding query names on the alias.
-#}
{% macro toorow_source_or_empty(source_name, table_name, columns) %}
    {%- set relation = source(source_name, table_name) -%}
    {%- if toorow_source_present(source_name, table_name) -%}
        {{ relation }}
    {%- else -%}
        {{- toorow_log_source_absent(source_name, [table_name]) -}}
        ({{ toorow_empty_projection(columns) }})
    {%- endif -%}
{% endmacro %}


{#-
  THE ONE LINE THAT KEEPS THE NIGHTLY HONEST.

  It is not decoration. A guarded model is BUILT, so dbt exits 0 having built
  models and `scheduler.run_dbt_per_project` would count the Project `ok` while
  every governed mart it produced is empty. That runner reads this marker and
  refuses the word: the text `TOOROW_SOURCE_ABSENT` is a contract between the two
  (`scheduler._dbt_guarded_models`), and renaming it here without renaming it
  there gives back a nightly that says `ok` over an empty warehouse.
-#}
{% macro toorow_log_source_absent(source_name, missing) %}
    {%- if execute and (missing | length) > 0 -%}
        {{- log("TOOROW_SOURCE_ABSENT model=" ~ this.identifier
              ~ " source=" ~ source_name
              ~ " relations=" ~ (missing | join(','))
              ~ " -- built EMPTY; the source has not been written to this warehouse",
              info=true) -}}
    {%- endif -%}
{% endmacro %}


{#-
  WHAT A MODEL -- OR A TEST -- SAYS WHEN ITS SOURCE IS NOT THERE: an empty
  relation that carries its columns, and the line that names what is missing.

  For a TEST the columns do not matter (dbt reads the row COUNT, and zero rows is
  a pass), so one column is enough: `[['declined', 'string']]`. What matters is
  that it declines OUT LOUD -- a test that quietly passes over a warehouse it
  could not measure is the vacuity its own cardinality guard exists to refuse.
-#}
{% macro toorow_absent_source_stub(source_name, missing, columns) %}
    {{- toorow_log_source_absent(source_name, missing) -}}
    {{ toorow_empty_projection(columns) }}
{% endmacro %}
