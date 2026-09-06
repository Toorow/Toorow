{% macro governed_alias_normalized(source_value) %}
{#-
  The SQL twin of `core.master_data.normalize_alias_value`, which is
  `" ".join(value.split()).casefold()` -- case folded, every run of whitespace
  collapsed to one space, and NOTHING else. Stripping punctuation or accents
  would make two genuinely different labels collide inside the UNIQUE index, and
  that index is what turns "one string names two identities" into a refusal
  rather than a race.

  KNOWN, BOUNDED DIVERGENCE. SQL `lower()` is not Python `casefold()`: casefold
  maps German sharp s to `ss` and folds a few ligatures, lower does not. A label
  carrying one of those characters therefore normalizes differently on the two
  sides. The consequence is stated rather than hidden: such a value finds no
  alias here, comes back UNRESOLVED, and the vocabulary guard reddens the build.
  It fails CLOSED -- it never resolves to the wrong node.

  AND IT IS SPELLED TWICE, for the reason `dbt/macros/engine_types.sql` states
  about types: this project builds on DuckDB locally and BigQuery in production
  (execution-substrate "Incomplete if" 23). `regexp_replace(x, '\s+', ' ', 'g')`
  is DuckDB's -- BigQuery has no fourth flags argument, replaces globally by
  default, and answers *Syntax error: Illegal escape sequence: \s* to the
  pattern unless it is a raw literal. Measured with a dry run at 0 bytes on
  2026-08-31: this macro was the one non-type construct criterion 23 named, and
  it refused `test_governed_node_resolution` on the production engine while
  building green locally. Both branches collapse the SAME runs of whitespace,
  which is what `core.master_data.normalize_alias_value` does in Python.
-#}
{%- if target.type == 'bigquery' -%}
lower(trim(REGEXP_REPLACE({{ source_value }}, r'\s+', ' ')))
{%- else -%}
lower(trim(regexp_replace({{ source_value }}, '\s+', ' ', 'g')))
{%- endif -%}
{% endmacro %}


{% macro _governed_alias_predicate(source_value, namespace, as_of_date, project_id) %}
{#- The one predicate both public macros share. Written once so the resolver and
    the state it reports can never disagree about what qualifies. -#}
    namespace = {{ namespace }}
    AND normalized_value = {{ governed_alias_normalized(source_value) }}
    AND effective_from <= {{ as_of_date }}
    AND (effective_to IS NULL OR effective_to > {{ as_of_date }})
    {%- if project_id %}
    -- An alias with no project is the ORG's, and reusable by every project of it;
    -- one that names a project belongs to that project alone. The nodes are
    -- project-scoped, the aliases are org-anchored, and this is the seam.
    AND (project_id IS NULL OR project_id = {{ project_id }})
    {%- endif %}
{% endmacro %}


{% macro resolve_governed_node(source_value, namespace, as_of_date, alias_relation, project_id=None) %}
{#
  resolve_governed_node: the governed node a source value names, or NULL.

  The third resolver of this project, and it reads a different authority from the
  other two on purpose:

    normalize_dimension    -> a dbt SEED (a platform vocabulary: countries, devices)
    MAP()                  -> `reference_tables` (the CLIENT's own vocabulary)
    resolve_governed_node  -> the mirrored MDM (a GOVERNED identity, with versions,
                              SKOS-typed aliases and a human decision behind it)

  FOUR REFUSALS, AND EACH ONE IS A DECISION SOMEBODY ELSE ALREADY TOOK.

  1. Only `relation = 'exact'` resolves. SKOS is explicit that a close match is
     not transitive, and `tracked_entities` records a similarity win as `close`,
     never `exact` -- "the mechanism by which two different companies become one
     row". Promoting close to exact here would undo that in one line.

  2. Only `conflict_state = 'none'` resolves. A collision or a contradiction is
     RECORDED upstream, never resolved by write order; picking a side here would
     be resolving it by query order instead, which is the same defect wearing a
     different hat.

  3. AMBIGUITY REFUSES, it does not pick. `uq_master_data_aliases_live_exact`
     covers only OPEN-ENDED aliases (`WHERE relation = 'exact' AND retired_at IS
     NULL AND effective_to IS NULL`), so two exact aliases with OVERLAPPING closed
     windows are a legal database state. A `LIMIT 1` would silently choose one of
     them by physical order. This returns NULL instead, and
     `governed_node_resolution_state` says `ambiguous` so the row can be repaired
     rather than believed.

  4. An unknown value returns NULL. Combined with a `not_null` test on the
     resolved column, an unknown value reddens the build -- the same vocabulary
     boundary `normalize_dimension` enforces, for the same reason.

  Retired aliases and archived nodes never appear: `master_data_aliases_dim_v`
  and `master_data_nodes_dim_v` filter them at the mirror (migration 233).
  Nothing is deleted upstream, so those WHERE clauses are the only thing that
  retires an identity.

  Args:
    source_value:   SQL expression for the raw value as the source spells it.
    namespace:      SQL expression for the namespace the value lives in -- a
                    connector, a locale catalogue, a client spreadsheet.
    as_of_date:     SQL expression for the date the resolution is made AT. A row
                    of a fact resolves at ITS OWN date, never at today: an alias
                    that starts next month must not rewrite last month's history.
    alias_relation: the alias relation -- `source('mirror',
                    'master_data_aliases_dim')` in a model, a seed in a test.
    project_id:     optional SQL expression scoping to one project.

  Usage:
    {{ resolve_governed_node("brand_raw", "'client_workbook'", "date",
                             source('mirror', 'master_data_aliases_dim'),
                             project_id='project_id') }}
#}
(
    SELECT CASE WHEN COUNT(*) = 1 THEN MIN(node_id) END
    FROM {{ alias_relation }}
    WHERE {{ _governed_alias_predicate(source_value, namespace, as_of_date, project_id) }}
      AND relation = 'exact'
      AND conflict_state = 'none'
)
{% endmacro %}


{% macro governed_node_resolution_state(source_value, namespace, as_of_date, alias_relation, project_id=None) %}
{#
  Why the value did not resolve, in one word, so an unresolved row can be
  repaired instead of merely counted.

    resolved    exactly one live exact alias, no conflict
    ambiguous   several qualify -- overlapping windows; a human picks
    conflicted  a candidate carries a collision or a contradiction
    inexact     the value is known, but only as close/broader/narrower/related
    unknown     nothing in this namespace names it

  `inexact` is worth its own word: it means the vocabulary HAS heard of the value
  and deliberately refused to call it an equality. Reporting that as `unknown`
  would send somebody to add an alias that already exists.

  Order matters: conflicted outranks ambiguous, which outranks inexact. A row
  reports the strongest reason it has, not the first one found.

  THE COUNTS ARE CONDITIONAL SUMS, not `COUNT(*) FILTER (WHERE ...)`. The FILTER
  clause is DuckDB's (and Postgres's); BigQuery answers *Expected keyword THEN
  but got keyword FILTER* and spells the same thing `COUNTIF(...)` -- measured
  with a dry run at 0 bytes on 2026-08-31. `SUM(CASE WHEN ... THEN 1 ELSE 0 END)`
  is the one form BOTH engines accept, so this needs no `target.type` branch at
  all: a construct that is already portable is better than a construct written
  twice (`dbt/macros/engine_types.sql` states the rule, and this is the case it
  does not apply to). `COALESCE` keeps the empty-relation answer at 0 rather
  than NULL, which is what COUNT returned.
#}
(
    SELECT CASE
        WHEN COALESCE(SUM(CASE WHEN conflict_state <> 'none' THEN 1 ELSE 0 END), 0) > 0
            THEN 'conflicted'
        WHEN COALESCE(SUM(CASE WHEN relation = 'exact' AND conflict_state = 'none'
                               THEN 1 ELSE 0 END), 0) > 1
            THEN 'ambiguous'
        WHEN COALESCE(SUM(CASE WHEN relation = 'exact' AND conflict_state = 'none'
                               THEN 1 ELSE 0 END), 0) = 1
            THEN 'resolved'
        WHEN COUNT(*) > 0 THEN 'inexact'
        ELSE 'unknown'
    END
    FROM {{ alias_relation }}
    WHERE {{ _governed_alias_predicate(source_value, namespace, as_of_date, project_id) }}
)
{% endmacro %}
