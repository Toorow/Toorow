-- T4 (Story 41.4, AC3) -- the BELT to T3's braces: the twins' ladder totals are pinned to
-- the figures computed from the THREE NON-VERIFICATION RULES ONLY.
--
-- T3 proves the twins are EQUAL TO EACH OTHER. That is the strong, assumption-free half,
-- but it has one blind spot by construction: if verification cost somehow entered BOTH
-- twins, they would still be equal. This test closes it by pinning the absolute value.
--
-- The arithmetic, from 12 345.67 EUR of net media and nothing else (Story 41.4 §E.3):
--     net_media_micros           12 345 670 000
--     platform 3 %                  370 370 100     ROUND(12 345 670 000 x 0.03)
--     regulatory / WHT                        0     REAL zeros -- no rule routes there
--     subtotal at phase 5 entry  12 716 040 100
--     agency 10 %                 1 271 604 010     ROUND(12 716 040 100 x 0.10)
--     subtotal_ht_micros         13 987 644 110
--     sales_tax 20 %              2 797 528 822     ROUND(13 987 644 110 x 0.20)
--     total_ttc_micros           16 785 172 932
--
-- Twin B's overlay simultaneously reports 5 000 000 000 micros of verification cost,
-- which appears NOWHERE in that chain. If someone ever folds verification into the
-- cascade, total_ttc_micros moves and this test names the number it moved to.
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
WITH twin_rows AS (
    SELECT
        project_id,
        date,
        breakdown_value,
        net_media_micros,
        subtotal_ht_micros,
        total_ttc_micros,
        is_ladder_complete
    FROM {{ ref('fee_tax_ladder_daily') }}
    WHERE project_id IN ('feetax_dev_verif_twin_a', 'feetax_dev_verif_twin_b')
      AND date            = CAST('2026-05-01' AS DATE)
      AND breakdown_value = 'fvt_camp_1'
),

cardinality_guard AS (
    -- EXACTLY ONE such row per twin: two would mean the canonical-series collapse failed
    -- and every figure below would be a multiple of the truth.
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: expected exactly one 2026-05-01 fvt_camp_1 ladder row per twin,'
        || ' got ' || CAST((SELECT COUNT(*) FROM twin_rows) AS STRING) || ' rows across'
        || ' both. Run dbt/seeds/feetax/seed_fee_tax_verification.py and rebuild'
        || ' fact_daily_kpi before the ladder.' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM twin_rows) <> 2
       OR (SELECT COUNT(DISTINCT project_id) FROM twin_rows) <> 2
),

total_breaks AS (
    SELECT
        t.project_id AS subject,
        'LADDER_TOTAL_PINNED_FAIL: net_media='
            || COALESCE(CAST(t.net_media_micros AS STRING), 'NULL') || ' (expected 12345670000)'
        || '  subtotal_ht=' || COALESCE(CAST(t.subtotal_ht_micros AS STRING), 'NULL')
            || ' (expected 13987644110)'
        || '  total_ttc=' || COALESCE(CAST(t.total_ttc_micros AS STRING), 'NULL')
            || ' (expected 16785172932).'
        || ' These figures come from the three NON-verification rules only; if they moved,'
        || ' either a rule changed or verification cost entered the cascade.'
            AS failure_reason
    FROM twin_rows t
    WHERE t.net_media_micros   IS DISTINCT FROM 12345670000
       OR t.subtotal_ht_micros IS DISTINCT FROM 13987644110
       OR t.total_ttc_micros   IS DISTINCT FROM 16785172932
),

completeness_break AS (
    -- The totals above are only readable when the ladder is complete; an incomplete
    -- ladder nulls them, and a NULL == NULL comparison would hide that.
    SELECT
        t.project_id AS subject,
        'LADDER_INCOMPLETE_FAIL: the twin ladder must compose fully (no rule constrains an'
        || ' unresolvable attribute), so a gap here means a VERIFICATION rule started'
        || ' raising one -- which is exactly what KEEP_SEPARATE forbids' AS failure_reason
    FROM twin_rows t
    WHERE NOT t.is_ladder_complete
)

SELECT subject, failure_reason FROM cardinality_guard
UNION ALL SELECT subject, failure_reason FROM total_breaks
UNION ALL SELECT subject, failure_reason FROM completeness_break
{%- endif -%}
