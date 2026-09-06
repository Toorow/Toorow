-- T9 (Story 41.3, AC8 / E41-FR05) -- cross-currency is REFUSED, never summed, never
-- converted.
--
-- There is no currency column on fact_daily_kpi: cost was already converted at read
-- (fx_convert_at_read, Epic 39.10), so a ladder row's currency IS the project's
-- canonical currency. The only place two currencies can meet is a RULE-DECLARED amount
-- (FLAT.amount_micros / CPM.cpm_micros). When they differ the component is refused:
-- gap_currency_mismatch, the amount summed NOWHERE, and NULL headline totals.
-- Converting instead would apply FX a SECOND time outside the read locus, which
-- [[fx-locus-read-not-staging]] and Epic 39.10 forbid -- so "we converted it for the
-- user's convenience" is the failure this test exists to catch.
--
-- ANTI-VACUITY GUARD: at least one confirmed FLAT/CPM rule with a currency different
-- from its project's canonical currency must exist, or nothing is being refused.
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
WITH mismatched_rules AS (
    SELECT r.rule_id, r.project_id, r.rule_currency, r.amount_micros, p.canonical_currency
    FROM {{ ref('fee_tax_rules_effective') }} r
    JOIN {{ source('mirror', 'project_preferences') }} p
        ON p.project_id = r.project_id
    WHERE r.form IN ('FLAT', 'CPM')
      AND COALESCE(r.rule_currency, p.canonical_currency) <> p.canonical_currency
      AND r.category IN ('PLATFORM_FEE', 'REGULATORY_TAX', 'WHT_GROSS_UP',
                         'AGENCY_FEE', 'SALES_TAX')
),

anti_vacuity_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'ANTI_VACUITY_FAIL: no confirmed FLAT/CPM rule declares a currency other than its'
        || ' project canonical currency -- the refusal path is never exercised'
            AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM mismatched_rules) = 0
),

refused_rows AS (
    SELECT * FROM {{ ref('fee_tax_ladder_daily') }} WHERE project_id = 'feetax_dev_refused'
),

cardinality_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: feetax_dev_refused produced no ladder row -- run'
        || ' seed_fee_tax_mirror.py before dbt build' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM refused_rows) = 0
),

not_refused AS (
    SELECT
        r.project_id || '|' || CAST(r.date AS STRING) || '|' || r.breakdown_value AS subject,
        'CURRENCY_FAIL: a FLAT rule in a foreign currency did not produce'
        || ' gap_currency_mismatch + a NULL phase column + NULL headline totals'
            AS failure_reason
    FROM refused_rows r
    WHERE NOT (r.gap_currency_mismatch
               AND r.gap_codes = 'CURRENCY_MISMATCH'
               AND r.platform_fee_micros IS NULL
               AND r.subtotal_ht_micros IS NULL
               AND r.total_ttc_micros IS NULL)
),

amount_leaked AS (
    -- The refused amount (500 000 000 micros) must appear in NO column of the row, in
    -- particular not converted and not silently added to the net media.
    SELECT
        r.project_id || '|' || CAST(r.date AS STRING) AS subject,
        'CURRENCY_FAIL: the refused amount leaked into a ladder column -- it must be'
        || ' summed nowhere and converted never' AS failure_reason
    FROM refused_rows r
    WHERE COALESCE(r.platform_fee_micros, 0) = 500000000
       OR r.net_media_micros <> 1000000000
       OR r.applied_rule_ids LIKE '%ftr_dev_refused_flat_usd%'
),

fixture_break AS (
    SELECT
        'cross_currency_refused' AS subject,
        'FIXTURE_FAIL: scenario cross_currency_refused must expect CURRENCY_MISMATCH,'
        || ' incomplete, and NULL headline totals' AS failure_reason
    FROM {{ ref('epic41_cascade_fixture') }} f
    WHERE f.scenario = 'cross_currency_refused'
      AND (COALESCE(f.expected_gap_codes, '') <> 'CURRENCY_MISMATCH'
           OR f.expected_is_complete <> FALSE
           OR f.expected_total_ttc_micros IS NOT NULL)
)

SELECT subject, failure_reason FROM anti_vacuity_guard
UNION ALL SELECT subject, failure_reason FROM cardinality_guard
UNION ALL SELECT subject, failure_reason FROM not_refused
UNION ALL SELECT subject, failure_reason FROM amount_leaked
UNION ALL SELECT subject, failure_reason FROM fixture_break
{%- endif -%}
