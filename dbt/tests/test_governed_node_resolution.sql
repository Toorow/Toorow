-- test_governed_node_resolution.sql -- Story 64.10 (AI-232).
--
-- THE RESOLVER MAY REFUSE. IT MAY NEVER PICK.
--
-- `resolve_governed_node` reads the mirrored MDM, which is the one authority of
-- the three where a wrong answer is worse than no answer: a seed vocabulary and a
-- client correspondence table say what a word means, this one says WHICH IDENTITY
-- a number is about. Every assertion below is a refusal the resolver must keep
-- making; each corresponds to a decision somebody else already took upstream.
--
-- IT REPLAYS THE MACRO OVER A VERSIONED SEED, for the reason
-- `test_country_bucket_absence.sql` states: a test that depends on rows landed by
-- a script somebody has to remember to run is RED on a fresh checkout for
-- everyone who does not know about the script. `dbt/seeds/governed_alias_fixture.csv`
-- is loaded by `dbt seed` in every environment, so the evidence travels with the
-- repository.
--
-- WHAT THE REPLAY CANNOT PROVE. That the mirror carries the same columns -- that
-- is `test_mirror_sync.py` (Story 64.9), which asserts the projection against the
-- migration text. Here the seed stands in for the mirror relation, so the two
-- tests must agree on the column list; a divergence shows up as a compile error
-- on this file, which is the intended failure mode.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

{% set as_of = "CAST('2026-06-15' AS DATE)" %}
{% set ns = "'__gov_ns__'" %}
{% set proj = "'proj_GOVALIAS'" %}

WITH probe AS (
    -- The value as a SOURCE spells it, what must come back, and why. The raw
    -- spellings are deliberately not the normalized ones: normalization is part
    -- of what is under test.
    {#- `VALUES` COMME TABLE AUTONOME NE TRAVERSE PAS (AI-314, 2026-08-24) :
        BigQuery repond *Expected keyword JOIN but got ","*, donc ce test etait
        REFUSE chaque nuit sur le moteur de la production (mesure : dry run a 0
        octet). Les onze lignes, leurs orthographes et leurs raisons sont
        inchangees : seule l ecriture bouge, en UNION de SELECT d une ligne. -#}
    {%- set probe_rows = [
        ('Chain One', "'mdnode_RESOLVED'", 'resolved',
         'one live exact alias, no conflict -- the only case that resolves'),
        ('  ChAiN   TwO  ', "'mdnode_SPACING'", 'resolved',
         'case folded and whitespace runs collapsed, exactly as normalize_alias_value does in Python'),
        ('Chain Three', 'NULL', 'inexact',
         'known only as `close` -- SKOS says a close match is not transitive, and promoting it here is how two companies become one row'),
        ('Chain Four', 'NULL', 'conflicted',
         'a contradiction is recorded upstream, never resolved by write order -- picking a side here resolves it by query order instead'),
        ('Chain Five', 'NULL', 'ambiguous',
         'two exact aliases with overlapping closed windows are a LEGAL state: uq_master_data_aliases_live_exact only covers effective_to IS NULL. LIMIT 1 would pick one by physical order'),
        ('Chain Six', 'NULL', 'unknown',
         'the window closed before the as-of date -- an alias resolves at the row date, never at today'),
        ('Chain Seven', 'NULL', 'unknown',
         'the window opens after the as-of date -- an alias starting next month must not rewrite last month'),
        ('Chain Eight', "'mdnode_ORGWIDE'", 'resolved',
         'an alias with no project belongs to the ORG and every project of it may use it -- nodes are project-scoped, aliases are org-anchored'),
        ('Chain Nine', 'NULL', 'unknown',
         'an alias naming ANOTHER project must not resolve here -- that would be a cross-project identity leak'),
        ('Chain Ten', 'NULL', 'unknown',
         'the same spelling in another namespace is another claim -- a connector and a client workbook are not one vocabulary'),
        ('Chain Eleven', 'NULL', 'unknown',
         'nothing names it -- NULL plus a not_null test is what reddens the build on a spelling nobody taught'),
    ] -%}
{%- for source_value, expected_node, expected_state, why in probe_rows %}
    {% if not loop.first %}UNION ALL {% endif %}SELECT
        '{{ source_value | replace("'", "''") }}' AS source_value,
        CAST({{ expected_node }} AS {{ dbt.type_string() }}) AS expected_node,
        '{{ expected_state }}' AS expected_state,
        '{{ why | replace("'", "''") }}' AS why
{%- endfor %}
),

replayed AS (
    SELECT
        source_value,
        expected_node,
        expected_state,
        why,
        {{ resolve_governed_node('source_value', ns, as_of,
                                 ref('governed_alias_fixture'), project_id=proj) }} AS resolved_node,
        {{ governed_node_resolution_state('source_value', ns, as_of,
                                          ref('governed_alias_fixture'), project_id=proj) }} AS state
    FROM probe
),

-- (a) ANTI-VACUITY, first, so nothing below can pass by having nothing to look at.
-- Two counts, because an emptied seed and an unloaded seed look identical from a
-- test that only counts probes.
cardinality_guard AS (
    SELECT
        CAST(NULL AS {{ toorow_string_type() }}) AS source_value,
        CAST(NULL AS {{ toorow_string_type() }}) AS expected,
        CAST(NULL AS {{ toorow_string_type() }}) AS measured,
        'CARDINALITY_FAIL: governed_alias_fixture holds fewer than 11 alias rows, '
        || 'or no probe resolved at all -- the seed did not run or was emptied'
                              AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM {{ ref('governed_alias_fixture') }}) < 11
       OR (SELECT COUNT(*) FROM replayed WHERE resolved_node IS NOT NULL) = 0
),

-- (b) THE NODE. Anything other than the expected identity -- including a node
-- where none was due, which is the only failure that produces a WRONG number
-- rather than a missing one.
wrong_node AS (
    SELECT
        source_value,
        COALESCE(expected_node, '<NULL>')  AS expected,
        COALESCE(resolved_node, '<NULL>')  AS measured,
        'WRONG_NODE: ' || why              AS failure_reason
    FROM replayed
    WHERE resolved_node IS DISTINCT FROM expected_node
),

-- (c) THE REPORTED REASON. An unresolved row that cannot say WHY is a row nobody
-- can repair: `inexact` sends somebody to review a close match, `unknown` sends
-- them to add an alias, and reporting the first as the second wastes the trip.
wrong_state AS (
    SELECT
        source_value,
        expected_state         AS expected,
        COALESCE(state, '<NULL>') AS measured,
        'WRONG_STATE: ' || why AS failure_reason
    FROM replayed
    WHERE state IS DISTINCT FROM expected_state
),

-- (d) THE TWO MACROS MAY NEVER DISAGREE. They share one predicate on purpose; if
-- a future edit splits them, a row could resolve to a node while reporting
-- `unknown`, or report `resolved` while returning NULL. Either way a reader
-- believes something the other half denies.
state_contradicts_node AS (
    SELECT
        source_value,
        expected_state                     AS expected,
        COALESCE(state, '<NULL>') || '/' || COALESCE(resolved_node, '<NULL>') AS measured,
        'STATE_CONTRADICTS_NODE: the resolver and the reason it reports disagree'
                                           AS failure_reason
    FROM replayed
    WHERE (state = 'resolved') <> (resolved_node IS NOT NULL)
)

SELECT * FROM cardinality_guard
UNION ALL
SELECT * FROM wrong_node
UNION ALL
SELECT * FROM wrong_state
UNION ALL
SELECT * FROM state_contradicts_node
