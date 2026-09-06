-- T15 (Story 41.3, AC12 / decision D2) -- an ambiguous datastream scope NEVER guesses.
--
-- fact_daily_kpi has no datastream_id, so a datastream-scoped rule can only be narrowed
-- to its CONNECTOR. If the project runs two datastreams on that connector, the
-- connector's rows mix both and there is no honest way to attribute them. The rule then
-- does not fire at all and the connector's rows carry DATASTREAM_SCOPE_AMBIGUOUS with
-- NULL headline totals. ONE IS NEVER PICKED -- picking one would produce a quietly wrong
-- invoice, which is strictly worse than a loud NULL.
--
-- Three cases, all seeded:
--   (a) feetax_dev_complete: exactly ONE datastream on the connector -> scope_state
--       RESOLVED and the rule FIRES (ftr_dev_complete_ds is in applied_rule_ids);
--   (b) feetax_dev_scope:    TWO datastreams -> scope_state AMBIGUOUS, the gap is raised,
--       totals NULL, and the rule's id appears in NO candidate row's applied_rule_ids;
--   (c) an unknown scope_ref -> scope_state UNRESOLVED and RULE_SCOPE_UNRESOLVED across
--       the whole project (we do not even know the connector, so the blast radius is the
--       project rather than one connector).
--
-- The rule-side half is asserted on fee_tax_rules_effective, which is deterministic from
-- the seeder; the ladder-side half is asserted on the rows that exist.
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
WITH scoped AS (
    SELECT rule_id, project_id, scope_kind, scope_state, scope_connector
    FROM {{ ref('fee_tax_rules_effective') }}
    WHERE scope_kind = 'datastream'
),

cardinality_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: no datastream-scoped rule exists, so D2 is untested. Run'
        || ' seed_fee_tax_mirror.py.' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM scoped) = 0
),

anti_vacuity_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'ANTI_VACUITY_FAIL: the seeded datastream scopes do not cover all three states'
        || ' (RESOLVED / AMBIGUOUS / UNRESOLVED)' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(DISTINCT scope_state) FROM scoped) < 3
),

wrong_state AS (
    SELECT r.rule_id AS subject,
           'SCOPE_STATE_FAIL: ' || r.rule_id || ' resolved to ' || r.scope_state
               AS failure_reason
    FROM scoped r
    WHERE (r.rule_id = 'ftr_dev_complete_ds'      AND r.scope_state <> 'RESOLVED')
       OR (r.rule_id = 'ftr_dev_scope_ambiguous'  AND r.scope_state <> 'AMBIGUOUS')
       OR (r.rule_id = 'ftr_dev_scope_unknown'    AND r.scope_state <> 'UNRESOLVED')
),

resolved_did_not_fire AS (
    SELECT
        l.project_id || '|' || CAST(l.date AS STRING) || '|' || l.breakdown_value AS subject,
        'SCOPE_FAIL: a datastream-scoped rule on a SINGLE-datastream connector did not'
        || ' fire -- RESOLVED must behave exactly like an unscoped rule on that connector'
            AS failure_reason
    FROM {{ ref('fee_tax_ladder_daily') }} l
    WHERE l.project_id = 'feetax_dev_complete'
      AND l.applied_rule_ids NOT LIKE '%ftr_dev_complete_ds%'
),

ambiguous_not_gapped AS (
    SELECT
        l.project_id || '|' || CAST(l.date AS STRING) || '|' || l.breakdown_value AS subject,
        'SCOPE_FAIL: TWO datastreams on the connector did not produce'
        || ' gap_datastream_scope_ambiguous + NULL totals (gap_codes=' || l.gap_codes || ')'
            AS failure_reason
    FROM {{ ref('fee_tax_ladder_daily') }} l
    WHERE l.project_id = 'feetax_dev_scope'
      AND NOT (l.gap_datastream_scope_ambiguous
               AND l.gap_rule_scope_unresolved
               AND l.subtotal_ht_micros IS NULL
               AND l.total_ttc_micros IS NULL)
),

ambiguous_picked_one AS (
    SELECT
        l.project_id || '|' || CAST(l.date AS STRING) || '|' || l.breakdown_value AS subject,
        'SCOPE_FAIL: the ambiguous rule was APPLIED anyway -- one datastream was picked,'
        || ' which D2 forbids outright' AS failure_reason
    FROM {{ ref('fee_tax_ladder_daily') }} l
    WHERE l.applied_rule_ids LIKE '%ftr_dev_scope_ambiguous%'
       OR l.applied_rule_ids LIKE '%ftr_dev_scope_unknown%'
)

SELECT subject, failure_reason FROM cardinality_guard
UNION ALL SELECT subject, failure_reason FROM anti_vacuity_guard
UNION ALL SELECT subject, failure_reason FROM wrong_state
UNION ALL SELECT subject, failure_reason FROM resolved_did_not_fire
UNION ALL SELECT subject, failure_reason FROM ambiguous_not_gapped
UNION ALL SELECT subject, failure_reason FROM ambiguous_picked_one
{%- endif -%}
