-- test_governed_node_age.sql -- Story 64.11 (AI-232).
--
-- AN AGE MAY BE UNKNOWN. IT MAY NEVER BE NEGATIVE.
--
-- This is the measure the whole epic was written to make possible. On the
-- derivation dossier the life curve is 2 300 views at two weeks, a plateau, then
-- 4 200 at four years: a three-day-old video and a four-year-old one occupy the
-- same column of a breakdown, and comparing them is a contresens. "Under-
-- performing" means nothing until it is said FOR ITS AGE.
--
-- WHY THE MACRO AND NOT A SEMANTIC EXPRESSION. `semantic_expressions` types
-- `subtract` over numeric families only (`_NUMERIC = {integer, decimal}`), and
-- `date` belongs to none -- `subtract(date, date)` is refused by
-- `_combine_additive`. `age_days` is therefore a Concept with a `source_measure`,
-- physically produced here by `days_between`, which already exists and is already
-- cross-adapter.
--
-- IT REPLAYS THE MACROS OVER A VERSIONED SEED, the reason
-- `test_country_bucket_absence.sql` gives in its own header: a test depending on
-- rows somebody has to remember to land is red on a fresh checkout for everyone
-- who does not know about the script.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

{% set as_of = "CAST('2026-06-15' AS DATE)" %}
{% set attr = "'born_at'" %}
{% set rel = ref('governed_attribute_fixture') %}

{#- `VALUES` COMME TABLE AUTONOME NE TRAVERSE PAS (AI-314, 2026-08-24) : BigQuery
    repond *Expected keyword JOIN but got ","*, donc ce test etait REFUSE chaque
    nuit sur le moteur de la production (mesure : dry run a 0 octet). Les sept
    lignes, leurs ages et leurs raisons sont inchanges : seule l ecriture bouge,
    en UNION de SELECT d une ligne. -#}
{%- set probe_rows = [
    ('mdnode_KNOWN', '151', 'known',
     'a readable birth date on 2026-01-15 -- 151 days before the as-of date'),
    ('mdnode_SAMEDAY', '0', 'known',
     'born ON the as-of date: an age of ZERO is a value, not an absence, and a NULL here would drop the day a video launched -- its highest-velocity day'),
    ('mdnode_UNBORN', 'NULL', 'unborn',
     'birth AFTER the as-of date. -78 is not a small error: it sums, averages and buckets like any other number, and timezone skew produces it routinely'),
    ('mdnode_MISTYPED', 'NULL', 'unknown',
     'the birth date was written in the wrong type -- Story 64.2 flags it rather than nulling it, and this macro must not read the flag as "no birth date given"'),
    ('mdnode_UNDECLARED', 'NULL', 'unknown',
     'the attribute was never declared by the object kind: a value nobody contracted for must not become an age'),
    ('mdnode_NOBIRTH', 'NULL', 'unknown',
     'the node carries other attributes and no birth one at all'),
    ('mdnode_ABSENT', 'NULL', 'unknown',
     'a node the projection knows nothing about'),
] -%}
WITH probe AS (
{%- for node_id, expected_age, expected_state, why in probe_rows %}
    {% if not loop.first %}UNION ALL {% endif %}SELECT
        '{{ node_id }}' AS node_id,
        CAST({{ expected_age }} AS {{ dbt.type_int() }}) AS expected_age,
        '{{ expected_state }}' AS expected_state,
        '{{ why | replace("'", "''") }}' AS why
{%- endfor %}
),

replayed AS (
    SELECT
        node_id,
        expected_age,
        expected_state,
        why,
        {{ governed_node_age_days(rel, 'probe.node_id', attr, as_of) }}  AS age_days,
        {{ governed_node_age_state(rel, 'probe.node_id', attr, as_of) }} AS state
    FROM probe
),

-- (a) ANTI-VACUITY. An emptied seed and an unloaded seed look identical to a test
-- that only counts probes, so both are checked.
cardinality_guard AS (
    SELECT
        CAST(NULL AS {{ toorow_string_type() }}) AS node_id,
        CAST(NULL AS {{ toorow_string_type() }}) AS expected,
        CAST(NULL AS {{ toorow_string_type() }}) AS measured,
        'CARDINALITY_FAIL: governed_attribute_fixture holds fewer than 7 rows, or no '
        || 'probe produced an age -- the seed did not run or was emptied'
                              AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM {{ rel }}) < 7
       OR (SELECT COUNT(*) FROM replayed WHERE age_days IS NOT NULL) = 0
),

-- (b) THE AGE. `IS DISTINCT FROM` so an unexpected NULL fails as loudly as a
-- wrong number -- with `<>`, every NULL row would pass by being unknown.
wrong_age AS (
    SELECT
        node_id,
        COALESCE(CAST(expected_age AS {{ toorow_string_type() }}), '<NULL>') AS expected,
        COALESCE(CAST(age_days AS {{ toorow_string_type() }}), '<NULL>')     AS measured,
        'WRONG_AGE: ' || why                              AS failure_reason
    FROM replayed
    WHERE age_days IS DISTINCT FROM expected_age
),

-- (c) THE REASON. `unborn` reported as `unknown` sends somebody to add a birth
-- date that is already there, and hides the real question: why does a fact row
-- predate the object it names.
wrong_state AS (
    SELECT
        node_id,
        expected_state            AS expected,
        COALESCE(state, '<NULL>') AS measured,
        'WRONG_STATE: ' || why    AS failure_reason
    FROM replayed
    WHERE state IS DISTINCT FROM expected_state
),

-- (d) NO NEGATIVE AGE SURVIVES, whatever the state machine says. Stated
-- separately from (b) because it is the invariant, not one probe's expectation:
-- a future edit that changed the guard would have to defeat this too.
negative_age AS (
    SELECT
        node_id,
        '>= 0'                        AS expected,
        CAST(age_days AS {{ toorow_string_type() }})     AS measured,
        'NEGATIVE_AGE: an age below zero sums and buckets like any other number'
                                      AS failure_reason
    FROM replayed
    WHERE age_days < 0
),

-- (e) THE TWO MACROS MAY NEVER DISAGREE. They share `governed_node_born_at`; if a
-- future edit splits them, a node could report `known` and return no age.
state_contradicts_age AS (
    SELECT
        node_id,
        expected_state                                            AS expected,
        COALESCE(state,'<NULL>') || '/' || COALESCE(CAST(age_days AS {{ toorow_string_type() }}),'<NULL>')
                                                                  AS measured,
        'STATE_CONTRADICTS_AGE: the age and the reason it reports disagree'
                                                                  AS failure_reason
    FROM replayed
    WHERE (state = 'known') <> (age_days IS NOT NULL)
)

SELECT * FROM cardinality_guard
UNION ALL
SELECT * FROM wrong_age
UNION ALL
SELECT * FROM wrong_state
UNION ALL
SELECT * FROM negative_age
UNION ALL
SELECT * FROM state_contradicts_age
