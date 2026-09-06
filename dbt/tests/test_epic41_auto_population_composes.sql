-- T21 (Story 41.3 x 41.2, review finding F1 -- CRITICAL) -- a CONFIRMED auto-populated
-- project composes a COMPLETE total.
--
-- WHAT F1 WAS. Every rule Story 41.2's auto-population emitted carried a
-- `source_type_scope` that the warehouse can NEVER resolve -- nothing writes
-- app.datastream_source_types, so attr_source_type is NULL on every row. The moment an
-- operator confirmed those proposals, 100 % of them would have evaluated UNRESOLVED:
-- totals NULL, gap_codes='SOURCE_TYPE_UNRESOLVED', on a CORRECTLY-CONFIGURED project.
-- The module would have shipped computing nothing, for everyone, silently.
--
-- WHY IT SURVIVED TO PRODUCTION SHAPE, which matters more than the bug: this seeder's
-- fixtures HAND-WROTE an approximation of what auto-population emits. A hand-written
-- approximation of a generator drifts from the generator, and did. So the fixture behind
-- this test is built by ITERATING 41.2's pure `build_auto_rule_payloads()` -- the single
-- source of truth -- and never by transcribing its output. If the generator starts
-- emitting a precondition the ladder cannot satisfy, THIS TEST GOES RED. That is the
-- entire point of it existing.
--
-- Asserted: with the module ON, a resolvable country, and the generator's own rules
-- inserted at status='confirmed', every ladder row is COMPLETE -- total_ttc_micros
-- NOT NULL and gap_codes = ''.
--
-- ON THE COUNTRY RUNG (recorded, because it is a real constraint, not a shortcut): the
-- fixture resolves its country through 41.2's PROJECT-POSTURE rung, not through a
-- `country` breakdown row. Rung 1 is STRUCTURALLY UNREACHABLE for a cost row -- no
-- connector in fact_daily_kpi emits breakdown_dimension='country' together with
-- metric='cost' (amendment A.2 / AI-51: GA4 and GSC carry country but never cost; meta,
-- tiktok and linkedin carry cost but never country), which is the very reason
-- COUNTRY_UNRESOLVED exists. Rung 2 needs datastream_country_binding_dim, which the
-- story forbids this seeder to populate. Rung 3 is the only reachable one.
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
WITH auto_rows AS (
    SELECT * FROM {{ ref('fee_tax_ladder_daily') }} WHERE project_id = 'feetax_dev_auto'
),

auto_rules AS (
    SELECT * FROM {{ ref('fee_tax_rules_effective') }}
    WHERE project_id = 'feetax_dev_auto' AND origin = 'auto_country'
),

cardinality_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: no ladder row for feetax_dev_auto -- run'
        || ' seed_fee_tax_mirror.py; without it the end-to-end auto-population claim is'
        || ' untested and F1 could recur' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM auto_rows) = 0
),

rules_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'ANTI_VACUITY_FAIL: feetax_dev_auto carries no confirmed origin=auto_country'
        || ' rule, so "a confirmed auto project composes" is vacuously true. The fixture'
        || ' must be built by ITERATING build_auto_rule_payloads(), never by hand.'
            AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM auto_rules) = 0
),

country_guard AS (
    -- If the country did not resolve, the completeness assertion below would be testing
    -- the gap path instead of the composition path. Fail explicitly and point at the
    -- seeder, which prints a WARNING when it cannot write the Epic-37 posture.
    SELECT
        CAST(NULL AS STRING) AS subject,
        'SETUP_FAIL: feetax_dev_auto did not resolve a country, so this test would be'
        || ' exercising the gap path instead of the composition path. Check for the'
        || ' seeder WARNING about mirror.project_preferences.local_markets not being'
        || ' string-writable in this warehouse.' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM auto_rows WHERE attr_country = 'FR') = 0
      AND (SELECT COUNT(*) FROM auto_rows) > 0
),

incomplete_breaks AS (
    SELECT
        a.project_id || '|' || CAST(a.date AS STRING) || '|' || a.breakdown_value AS subject,
        'AUTO_POPULATION_FAIL: a CONFIRMED auto-populated rule set must compose a'
        || ' COMPLETE total on a correctly-configured project. Got gap_codes='
        || a.gap_codes || ' total_ttc='
        || COALESCE(CAST(a.total_ttc_micros AS STRING), 'NULL')
        || '. If gap_codes is SOURCE_TYPE_UNRESOLVED, the generator has reintroduced an'
        || ' unsatisfiable precondition -- that is finding F1 recurring.' AS failure_reason
    FROM auto_rows a
    WHERE NOT (a.total_ttc_micros IS NOT NULL AND a.gap_codes = '')
),

rules_fired_breaks AS (
    -- Complete is necessary but not sufficient: the auto rules must actually have FIRED.
    -- A rule set that matched nothing would also be "complete".
    SELECT
        a.project_id || '|' || CAST(a.date AS STRING) || '|' || a.breakdown_value AS subject,
        'AUTO_POPULATION_FAIL: the row is complete but only '
        || CAST(a.applied_rule_count AS STRING) || ' rule(s) fired, expected '
        || CAST((SELECT COUNT(*) FROM auto_rules) AS STRING)
        || ' -- a rule set that matches nothing is "complete" for the wrong reason'
            AS failure_reason
    FROM auto_rows a
    WHERE a.applied_rule_count <> (SELECT COUNT(*) FROM auto_rules)
)

SELECT subject, failure_reason FROM cardinality_guard
UNION ALL SELECT subject, failure_reason FROM rules_guard
UNION ALL SELECT subject, failure_reason FROM country_guard
UNION ALL SELECT subject, failure_reason FROM incomplete_breaks
UNION ALL SELECT subject, failure_reason FROM rules_fired_breaks
{%- endif -%}
