{% macro governed_node_born_at(attributes_relation, node_id, attribute_name) %}
{#
  The birth date a governed node carries, or NULL.

  Reads the typed projection of Story 64.2, so it inherits that view's whole
  discipline: `value_date` is populated ONLY where the object kind's contract
  declares the attribute as a date AND the payload spells one. A property the
  contract never declared, or one written in the wrong type, has a NULL
  `value_date` and a `TRUE` flag beside it -- and both are excluded here rather
  than silently read as "no birth date".

  WHY THE ATTRIBUTE IS NAMED BY THE CALLER. Nothing in this repository may decide
  that a client's birth attribute is called `born_at`: the client names its own
  properties (Story 64.1), and a macro that hardcoded a name would be the
  country-shaped door of this layer.

  THE CALLER MUST QUALIFY `node_id`, AND THIS IS NOT A STYLE NOTE. SQL resolves an
  unqualified name to the INNERMOST scope, so passing the bare string `node_id`
  makes the correlation read `_gna.node_id = _gna.node_id` -- always true. The
  subquery then returns MIN over EVERY row of the relation, and every node gets
  the same age with no error anywhere. Measured while writing
  `test_governed_node_age.sql`: seven probes, seven identical ages of 151 days.
  The inner relation is aliased `_gna` so the left side is unambiguous; the right
  side is the caller's, and only the caller can qualify it -- pass
  `'probe.node_id'`, `'f.node_id'`, never `'node_id'`.

  Args:
    attributes_relation: source('mirror','master_data_node_attributes_dim') in a
                         model, a seed in a test.
    node_id:             QUALIFIED SQL expression for the node whose age is wanted.
    attribute_name:      SQL expression naming the declared birth attribute.
#}
(
    SELECT MIN(_gna.value_date)
    FROM {{ attributes_relation }} AS _gna
    WHERE _gna.node_id = {{ node_id }}
      AND _gna.attribute = {{ attribute_name }}
      AND NOT _gna.undeclared
      AND NOT _gna.type_mismatch
      AND _gna.value_date IS NOT NULL
)
{% endmacro %}


{% macro governed_node_age_days(attributes_relation, node_id, attribute_name, as_of_date) %}
{#
  How many days old a governed node is at a given date, or NULL.

  THE MEASURE THIS EPIC WAS WRITTEN TO MAKE POSSIBLE. On the derivation dossier
  the life curve is 2 300 views at two weeks, a plateau, 4 200 at four years -- so
  a three-day-old video and a four-year-old one are not comparable, and
  "underperforming" means nothing until it is said FOR ITS AGE.

  WHY THIS IS NOT AN EXPRESSION IN THE SEMANTIC LAYER. `semantic_expressions`
  types `subtract` over numeric families only (`_NUMERIC = {integer, decimal}`);
  `date` belongs to none, so `subtract(date, date)` is REFUSED by
  `_combine_additive`. The contract has no date arithmetic, and widening the
  grammar for a case dbt already handles would be the larger change. `age_days` is
  therefore a Concept with a `source_measure` -- a physical measure produced HERE,
  by the `days_between` macro that already exists and is already cross-adapter.

  TWO REFUSALS, BOTH RETURNING NULL RATHER THAN A NUMBER:

  * no readable birth date -- see `governed_node_born_at`;
  * a birth date AFTER the as-of date. An age of -1 is not a small error, it is a
    value that sums, averages and buckets like any other. Publication skew across
    timezones produces it routinely, and a negative age in a cohort silently drags
    the whole bucket down.

  `governed_node_age_state` says WHICH of the two happened, so a NULL age can be
  repaired instead of merely counted.
#}
(
    CASE
        WHEN {{ governed_node_born_at(attributes_relation, node_id, attribute_name) }} IS NULL
            THEN NULL
        WHEN {{ governed_node_born_at(attributes_relation, node_id, attribute_name) }}
             > {{ as_of_date }}
            THEN NULL
        ELSE {{ days_between(
                    governed_node_born_at(attributes_relation, node_id, attribute_name),
                    as_of_date) }}
    END
)
{% endmacro %}


{% macro governed_node_age_state(attributes_relation, node_id, attribute_name, as_of_date) %}
{#
  Why an age is missing, in one word.

    known    a readable birth date, on or before the as-of date
    unborn   the birth date is AFTER the as-of date -- a row that exists before
             its object did, which is a fact about the data and not an error to
             swallow
    unknown  no readable birth date: never declared, mistyped, or absent

  `unborn` deserves its own word. Reporting it as `unknown` would send somebody to
  add a birth date that is already there, and hide the real question -- why does a
  fact row predate the object it names.
#}
(
    CASE
        WHEN {{ governed_node_born_at(attributes_relation, node_id, attribute_name) }} IS NULL
            THEN 'unknown'
        WHEN {{ governed_node_born_at(attributes_relation, node_id, attribute_name) }}
             > {{ as_of_date }}
            THEN 'unborn'
        ELSE 'known'
    END
)
{% endmacro %}
