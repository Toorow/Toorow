-- T17 (Story 41.3, ruling R2 / arbitration B1 / scenario S12) -- THE HONEST END STATE.
--
-- This is the "what does the module actually show on day one?" test, and it is a
-- SPECIFICATION, not a bug report. The country ladder's row-breakdown rung never fires
-- on cost sources (no cost-emitting connector lands a country breakdown) and its
-- declared-binding rung is unpopulated (migration 104 is not applied and nothing writes
-- binding_kind='datastream'). Story 41.2's project-posture rung rescues single-tracked-
-- country projects; a `global` or multi-country project keeps attr_country NULL. Since
-- 41.2's auto-population produces COUNTRY-CONDITIONED rules by construction (DST GB 2 %,
-- DST FR 3 %, VAT FR 20 %), those projects legitimately show:
--
--     net_media_micros        populated   (a real fact, always)
--     agency_fee_micros       populated   (the UNCONDITIONED rule fires normally)
--     regulatory_tax_micros   NULL        (both DST rules unresolvable)
--     sales_tax_micros        NULL        (the VAT rule unresolvable)
--     subtotal_ht / total_ttc NULL
--     gap_codes               'COUNTRY_UNRESOLVED'
--     is_ladder_complete      FALSE
--
-- and in the rollup: total_ttc_micros NULL, total_ttc_micros_complete_only 0,
-- complete_row_count 0, total_row_count 6, the country key '__unresolved__'.
--
-- ANTI-VACUITY GUARD, and it is the whole point of the scenario: the fixture project
-- must contain at least one UNCONDITIONED rule that DOES fire, so the test cannot pass
-- by everything simply being NULL. "Partial but honest" is the specified answer;
-- "blank" is not.
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
WITH gapped AS (
    SELECT * FROM {{ ref('fee_tax_ladder_daily') }} WHERE project_id = 'feetax_dev_gapped'
),

cardinality_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: expected 6 ladder rows for feetax_dev_gapped (2 campaigns x 3'
        || ' days), got ' || CAST((SELECT COUNT(*) FROM gapped) AS STRING) AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM gapped) <> 6
),

anti_vacuity_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'ANTI_VACUITY_FAIL: no row of feetax_dev_gapped has a POPULATED component --'
        || ' the scenario would then pass by everything being NULL, which is exactly the'
        || ' "blank product" outcome C5 exists to distinguish from an honest partial one'
            AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM gapped WHERE agency_fee_micros IS NOT NULL) = 0
),

row_breaks AS (
    SELECT
        g.project_id || '|' || CAST(g.date AS STRING) || '|' || g.breakdown_value AS subject,
        'HONEST_END_STATE_FAIL: expected net media + agency populated, DST and VAT NULL,'
        || ' totals NULL, gap_codes=COUNTRY_UNRESOLVED. Got agency='
        || COALESCE(CAST(g.agency_fee_micros AS STRING), 'NULL')
        || ' regulatory=' || COALESCE(CAST(g.regulatory_tax_micros AS STRING), 'NULL')
        || ' sales=' || COALESCE(CAST(g.sales_tax_micros AS STRING), 'NULL')
        || ' gap_codes=' || g.gap_codes AS failure_reason
    FROM gapped g
    WHERE NOT (g.net_media_micros IS NOT NULL
               AND g.agency_fee_micros IS NOT NULL
               AND g.agency_fee_micros > 0
               AND g.regulatory_tax_micros IS NULL
               AND g.sales_tax_micros IS NULL
               AND g.subtotal_ht_micros IS NULL
               AND g.total_ttc_micros IS NULL
               AND g.gap_codes = 'COUNTRY_UNRESOLVED'
               AND NOT g.is_ladder_complete)
),

rollup_breaks AS (
    SELECT
        r.project_id || '|' || CAST(r.date AS STRING) || '|' || r.rollup_kind
            || '|' || r.rollup_key AS subject,
        'HONEST_END_STATE_FAIL: the rollup must report 0 of N rather than a blank or a'
        || ' lie -- total_ttc NULL, complete_only 0, complete_row_count 0'
            AS failure_reason
    FROM {{ ref('fee_tax_ladder_rollup') }} r
    WHERE r.project_id = 'feetax_dev_gapped'
      AND NOT (r.total_ttc_micros IS NULL
               AND r.total_ttc_micros_complete_only = 0
               AND r.complete_row_count = 0
               AND r.total_row_count > 0
               AND NOT r.is_ladder_complete)
),

global_count_break AS (
    SELECT
        'feetax_dev_gapped|global' AS subject,
        'HONEST_END_STATE_FAIL: the global rollup must count all 6 ladder rows'
            AS failure_reason
    FROM {{ ref('fee_tax_ladder_rollup') }} r
    WHERE r.project_id = 'feetax_dev_gapped'
      AND r.rollup_kind = 'global'
    GROUP BY r.project_id
    HAVING SUM(r.total_row_count) <> 6
),

unresolved_bucket_break AS (
    SELECT
        'feetax_dev_gapped|country' AS subject,
        'HONEST_END_STATE_FAIL: the country rollup must key the unresolved rows to'
        || ' __unresolved__, never drop them and never bucket them into a real country'
            AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (
        SELECT COUNT(*)
        FROM {{ ref('fee_tax_ladder_rollup') }}
        WHERE project_id = 'feetax_dev_gapped'
          AND rollup_kind = 'country'
          AND rollup_key <> '__unresolved__'
    ) > 0
)

SELECT subject, failure_reason FROM cardinality_guard
UNION ALL SELECT subject, failure_reason FROM anti_vacuity_guard
UNION ALL SELECT subject, failure_reason FROM row_breaks
UNION ALL SELECT subject, failure_reason FROM rollup_breaks
UNION ALL SELECT subject, failure_reason FROM global_count_break
UNION ALL SELECT subject, failure_reason FROM unresolved_bucket_break
{%- endif -%}
