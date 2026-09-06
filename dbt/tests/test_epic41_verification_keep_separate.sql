-- T14 (Story 41.3, AC11 / Epic 27 invariant 4) -- VERIFICATION stays KEEP_SEPARATE.
--
-- IAS / DV verification cost is NOT part of the media cost ladder: it is a separate
-- concern with its own allocation base (measured impressions), and Story 41.4 owns it as
-- an OVERLAY. A confirmed VERIFICATION rule must therefore contribute exactly nothing to
-- every ladder column and -- just as important -- must raise NO GAP: it is excluded
-- BEFORE condition evaluation, so it can neither add nor block. A VERIFICATION rule that
-- gapped a row would be almost as wrong as one that summed into it, because it would
-- null the totals of every project that runs verification.
--
-- Also asserts that no VERIFICATION rule id ever appears in applied_rule_ids, and that
-- PAYMENT_FEE (the revenue side, Story 41.5) is admitted by no phase either.
--
-- ANTI-VACUITY GUARD: at least one confirmed VERIFICATION rule must exist for a
-- module-ON project, or nothing is being kept separate.
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
WITH verification_rules AS (
    SELECT rule_id, project_id
    FROM {{ ref('fee_tax_rules_effective') }}
    WHERE category IN ('VERIFICATION', 'PAYMENT_FEE')
),

anti_vacuity_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'ANTI_VACUITY_FAIL: no confirmed VERIFICATION rule exists for a module-ON'
        || ' project, so the KEEP_SEPARATE claim is untested. Run'
        || ' seed_fee_tax_mirror.py.' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM verification_rules) = 0
),

applied_leak AS (
    SELECT
        v.rule_id AS subject,
        'KEEP_SEPARATE_FAIL: a VERIFICATION / PAYMENT_FEE rule id appears in'
        || ' applied_rule_ids -- it must contribute nothing to the media cost ladder'
            AS failure_reason
    FROM {{ ref('fee_tax_ladder_daily') }} l
    JOIN verification_rules v
        ON  v.project_id = l.project_id
        AND l.applied_rule_ids LIKE '%' || v.rule_id || '%'
),

gap_leak AS (
    -- feetax_dev_complete's only "unusual" rule is the VERIFICATION CPM one. If
    -- verification were routed through the matcher instead of being excluded up front,
    -- these rows would gap and the project would lose its totals.
    SELECT
        l.project_id || '|' || CAST(l.date AS STRING) || '|' || l.breakdown_value AS subject,
        'KEEP_SEPARATE_FAIL: the project carrying the VERIFICATION rule is incomplete'
        || ' (gap_codes=' || l.gap_codes || ') -- a KEEP_SEPARATE rule must raise no gap'
            AS failure_reason
    FROM {{ ref('fee_tax_ladder_daily') }} l
    WHERE l.project_id = 'feetax_dev_complete'
      AND NOT l.is_ladder_complete
)

SELECT subject, failure_reason FROM anti_vacuity_guard
UNION ALL SELECT subject, failure_reason FROM applied_leak
UNION ALL SELECT subject, failure_reason FROM gap_leak
{%- endif -%}
