-- T8 (Story 41.3, AC3 / AC4 / arbitration B1) -- THE DISCRIMINANT.
--
-- Amendment B1, verbatim: "a condition that is known false evaluates to +0 micros; an
-- attribute that is unresolvable is a gap." Those two must never collapse into each
-- other -- one produces a real, trustworthy total, the other must refuse to produce one.
--
--   KNOWN FALSE  (feetax_dev_plan's UNMAPPED campaign, and fixture S2):
--       component exactly 0, is_ladder_complete TRUE, gap_codes = '',
--       headline totals NOT NULL.
--   UNRESOLVABLE (feetax_dev_gapped, and fixture S3 / S4):
--       component NULL, is_ladder_complete FALSE, the exact typed gap code,
--       and BOTH subtotal_ht_micros AND total_ttc_micros NULL.
--
-- Includes CONDITION_KEY_UNKNOWN: a rule referencing a key outside the recognised six
-- is UNRESOLVED, never "no match, move on".
--
-- CARDINALITY GUARDS on both halves: if either side has no rows the discriminant is
-- vacuous, which is precisely the failure mode this test exists to prevent.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

{#- UNE PREMISSE ABSENTE N EST PAS UN DEFAUT (AI-314, 2026-08-24).
    Ce test epingle un exemple SEME : il mesure ce que la fixture locale porte, et
    la fixture vit dans le MIROIR. `mirror_sync` differe ses ecritures BigQuery
    (Phase B), donc dans un entrepot ou le miroir n a pas ete ecrit -- toute la
    production aujourd hui -- ce test ne trouve rien a mesurer et rend son
    CARDINALITY_FAIL : un rouge qui accuse le calcul d un defaut dont la cause est
    qu il n y a rien a calculer. Un test rouge est un code de sortie, et un code
    de sortie est un projet sans marts.
    Il DECLINE donc de juger, EN LE DISANT : `TOOROW_SOURCE_ABSENT` remonte au
    nocturne, qui refuse alors le mot << ok >> pour ce projet. La ou le miroir EST
    -- la boucle locale, la CI -- rien ne bouge et l assertion reste entiere. -#}
{%- set mirror_missing = toorow_absent_sources('mirror', ['project_tax_fee_activation']) -%}
{%- if mirror_missing | length > 0 %}
{{ toorow_absent_source_stub('mirror', mirror_missing, [['declined', 'string']]) }}
{%- else %}
WITH known_false_rows AS (
    SELECT *
    FROM {{ ref('fee_tax_ladder_daily') }}
    WHERE project_id = 'feetax_dev_plan'
      AND breakdown_value = 'fdp_camp_unmapped'
),

unresolvable_rows AS (
    SELECT *
    FROM {{ ref('fee_tax_ladder_daily') }}
    WHERE project_id = 'feetax_dev_gapped'
),

guard_known_false AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: no known-false ladder row (feetax_dev_plan /'
        || ' fdp_camp_unmapped) -- the B1 discriminant has only one half' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM known_false_rows) = 0
),

guard_unresolvable AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: no unresolvable ladder row (feetax_dev_gapped) -- the B1'
        || ' discriminant has only one half' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM unresolvable_rows) = 0
),

known_false_breaks AS (
    SELECT
        k.project_id || '|' || CAST(k.date AS STRING) || '|' || k.breakdown_value AS subject,
        'KNOWN_FALSE_FAIL: an unmapped plan-scoped rule must contribute exactly 0 and'
        || ' leave the row COMPLETE (D5: the operator scoped the rule to a plan, so'
        || ' spend outside the plan legitimately is not covered) -- got platform='
        || COALESCE(CAST(k.platform_fee_micros AS STRING), 'NULL')
        || ' gap_codes=' || k.gap_codes AS failure_reason
    FROM known_false_rows k
    WHERE NOT (k.platform_fee_micros = 0
               AND k.is_ladder_complete
               AND k.gap_codes = ''
               AND k.subtotal_ht_micros IS NOT NULL
               AND k.total_ttc_micros IS NOT NULL)
),

unresolvable_breaks AS (
    SELECT
        u.project_id || '|' || CAST(u.date AS STRING) || '|' || u.breakdown_value AS subject,
        'UNRESOLVABLE_FAIL: a country-conditioned rule on a row with no resolved country'
        || ' must yield a NULL component, gap_codes=COUNTRY_UNRESOLVED and NULL headline'
        || ' totals -- got gap_codes=' || u.gap_codes AS failure_reason
    FROM unresolvable_rows u
    WHERE NOT (u.regulatory_tax_micros IS NULL
               AND u.gap_country_unresolved
               AND u.gap_codes = 'COUNTRY_UNRESOLVED'
               AND NOT u.is_ladder_complete
               AND u.subtotal_ht_micros IS NULL
               AND u.total_ttc_micros IS NULL)
),

source_type_breaks AS (
    -- Story 41.2's bridge emits attr_source_type NULL on EVERY row today (nothing
    -- writes app.datastream_source_types yet, and 41.2 deliberately refused to
    -- fabricate 'UNKNOWN' in SQL). A matcher that read NULL as KNOWN-FALSE would make
    -- every source-type-scoped rule compute NOTHING while reporting a complete,
    -- trustworthy total -- strictly worse than a loud gap. NULL must read UNRESOLVED.
    SELECT
        l.project_id || '|' || CAST(l.date AS STRING) || '|' || l.breakdown_value AS subject,
        'SOURCE_TYPE_FAIL: a source_type-scoped rule on a row with attr_source_type NULL'
        || ' must raise SOURCE_TYPE_UNRESOLVED and NULL the totals, never silently'
        || ' evaluate to known-false. Got gap_codes=' || l.gap_codes
        || ' platform=' || COALESCE(CAST(l.platform_fee_micros AS STRING), 'NULL')
            AS failure_reason
    FROM {{ ref('fee_tax_ladder_daily') }} l
    WHERE l.project_id = 'feetax_dev_source_type'
      AND NOT (l.gap_source_type_unresolved
               AND l.gap_codes = 'SOURCE_TYPE_UNRESOLVED'
               AND l.platform_fee_micros IS NULL
               AND l.subtotal_ht_micros IS NULL
               AND l.total_ttc_micros IS NULL
               AND l.applied_rule_ids = '')
),

guard_source_type AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: no ladder row for feetax_dev_source_type -- the'
        || ' NULL-source-type-is-UNRESOLVED case is untested' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (
        SELECT COUNT(*) FROM {{ ref('fee_tax_ladder_daily') }}
        WHERE project_id = 'feetax_dev_source_type'
    ) = 0
),

fixture_breaks AS (
    -- S2 must be complete with a zero component; S3 / S4 must be incomplete with their
    -- exact typed codes. Asserted on the fixture's own expectations so an edit that
    -- blurred the distinction is caught even before the models run.
    SELECT
        f.scenario AS subject,
        'FIXTURE_FAIL: known_false_de must be complete with gap_codes empty'
            AS failure_reason
    FROM {{ ref('epic41_cascade_fixture') }} f
    WHERE f.scenario = 'known_false_de'
      AND (f.expected_is_complete <> TRUE OR COALESCE(f.expected_gap_codes, '') <> '')
    UNION ALL
    SELECT
        f.scenario,
        'FIXTURE_FAIL: ' || f.scenario || ' must be incomplete with a typed gap code and'
        || ' NULL headline totals'
    FROM {{ ref('epic41_cascade_fixture') }} f
    WHERE f.scenario IN ('unresolvable_country', 'unresolvable_placement',
                         'unresolvable_source_type')
      AND (f.expected_is_complete <> FALSE
           OR COALESCE(f.expected_gap_codes, '') = ''
           OR f.expected_subtotal_ht_micros IS NOT NULL
           OR f.expected_total_ttc_micros IS NOT NULL)
    UNION ALL
    SELECT
        'unresolvable_placement',
        'FIXTURE_FAIL: the CONDITION_KEY_UNKNOWN case is missing -- an unrecognised'
        || ' condition key must never be silently ignored'
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (
        SELECT COUNT(*)
        FROM {{ ref('epic41_cascade_fixture') }}
        WHERE COALESCE(expected_gap_codes, '') = 'CONDITION_KEY_UNKNOWN'
    ) = 0
)

SELECT subject, failure_reason FROM guard_known_false
UNION ALL SELECT subject, failure_reason FROM guard_unresolvable
UNION ALL SELECT subject, failure_reason FROM guard_source_type
UNION ALL SELECT subject, failure_reason FROM known_false_breaks
UNION ALL SELECT subject, failure_reason FROM unresolvable_breaks
UNION ALL SELECT subject, failure_reason FROM source_type_breaks
UNION ALL SELECT subject, failure_reason FROM fixture_breaks
{%- endif -%}
