-- T7 (Story 41.3, AC2 / E41-AD9, epic AC2) -- per-ROW rates, no leakage, and the
-- empty-conditions LEFT-JOIN trap.
--
-- Three things, all of which a plausible wrong implementation gets wrong:
--
-- 1. THE EMPTY-CONDITIONS FAST PATH. A rule with NO row in
--    mirror.fee_tax_rule_conditions is UNCONSTRAINED and must fire on EVERY row. If the
--    matcher used an INNER JOIN instead of LEFT JOIN + COALESCE(state, 0) it would
--    silently delete every unconditional rule -- i.e. most rules in the platform -- and
--    still produce a plausible-looking, fully "complete", badly wrong invoice.
--    feetax_dev_complete carries five unconditioned/scoped rules that must ALL fire on
--    ALL six of its rows.
--
-- 2. THE BRIDGE gap_code IS NOT PROPAGATED (§D.3.1 trap). Story 41.2's view emits
--    gap_code='COUNTRY_UNRESOLVED' on essentially every paid-media row. A project whose
--    rules constrain nothing unresolvable must nevertheless stay COMPLETE. Copying the
--    bridge's gap_code onto the ladder row would null the totals of every project on the
--    platform, and this assertion is what catches it.
--
-- 3. NO LEAKAGE ACROSS ROWS. The fixture's S1 (VAT 20 % + DST 3 %) and S2 (VAT 5.5 % +
--    DST 0 %) sit in the SAME dataset with different attributes, and their expected
--    totals must differ -- if a future edit made them agree, the heterogeneity claim
--    would be untested.
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
WITH complete_project AS (
    SELECT *
    FROM {{ ref('fee_tax_ladder_daily') }}
    WHERE project_id = 'feetax_dev_complete'
),

cardinality_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: expected 6 ladder rows for feetax_dev_complete (2 campaigns'
        || ' x 3 days), got ' || CAST((SELECT COUNT(*) FROM complete_project) AS STRING)
            AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM complete_project) <> 6
),

unconditional_not_fired AS (
    -- 5 rules must fire on every row: platform 3 %, the datastream-scoped 1 %, WHT 15 %,
    -- agency 10 %, VAT 20 %. The proposed rule and the VERIFICATION rule must not.
    SELECT
        c.project_id || '|' || CAST(c.date AS STRING) || '|' || c.breakdown_value AS subject,
        'EMPTY_CONDITIONS_FAIL: expected 5 applied rules, got '
            || CAST(c.applied_rule_count AS STRING)
            || ' (' || c.applied_rule_ids || ') -- an unconditional rule must match every'
            || ' row; an INNER JOIN in the matcher would delete them all' AS failure_reason
    FROM complete_project c
    WHERE c.applied_rule_count <> 5
),

bridge_gap_propagated AS (
    SELECT
        c.project_id || '|' || CAST(c.date AS STRING) || '|' || c.breakdown_value AS subject,
        'BRIDGE_GAP_LEAK_FAIL: a project whose rules constrain NOTHING unresolvable is'
        || ' incomplete (gap_codes=' || c.gap_codes || ') -- the bridge gap_code must be'
        || ' provenance only and must never drive is_ladder_complete' AS failure_reason
    FROM complete_project c
    WHERE NOT c.is_ladder_complete
),

fixture_heterogeneity AS (
    SELECT
        'ladder_full_fr|known_false_de' AS subject,
        'HETEROGENEITY_FAIL: S1 and S2 no longer carry different sales-tax outcomes, so'
        || ' the "each row gets its exact rate" claim is untested' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (
        SELECT COUNT(DISTINCT expected_sales_tax_micros)
        FROM {{ ref('epic41_cascade_fixture') }}
        WHERE scenario IN ('ladder_full_fr', 'known_false_de')
    ) < 2
)

SELECT subject, failure_reason FROM cardinality_guard
UNION ALL SELECT subject, failure_reason FROM unconditional_not_fired
UNION ALL SELECT subject, failure_reason FROM bridge_gap_propagated
UNION ALL SELECT subject, failure_reason FROM fixture_heterogeneity
{%- endif -%}
