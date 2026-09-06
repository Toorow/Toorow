-- T1 (Story 41.4, AC1 / E41-FR04) -- the allocation is EXACT, INTEGER, and PROPORTIONAL
-- to measured impressions, with ZERO ALLOCATION RESIDUE.
--
-- Two halves, deliberately:
--   * a REPLAY of fee_tax_cpm_micros over epic41_verification_fixture. That seed can
--     never reach fact_daily_kpi (dbt UNIONs module STAGING, never seeds), so it pins the
--     MACRO's arithmetic in isolation -- including the two fail-closed guards, where the
--     expected value is deliberately NULL rather than 0.
--   * the LIVE model on feetax_dev_verif_twin_b, where the same arithmetic has to survive
--     the whole model: activation, the canonical series, rule selection, the window, the
--     matcher, the slot pick and the gap ledger.
--
-- WHY PROPORTIONALITY IS ASSERTED IN INTEGERS. cost_x * imp_y = cost_y * imp_x is the
-- cross-multiplied form of cost_x/imp_x = cost_y/imp_y, and it needs NO DIVISION -- so
-- the assertion itself cannot introduce the float step the engine spent so much effort
-- avoiding. At fixture magnitudes the products are ~2.4e15, far inside BIGINT.
--
-- ZERO RESIDUE is the property that makes "allocated BY measured impressions" true rather
-- than merely plausible: because CPM is LINEAR, the per-campaign figures ARE the
-- allocation and their integer SUM IS the day total. There is no separately-computed day
-- total for them to disagree with, and no remainder to dump on an arbitrary campaign.
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
WITH fixture AS (
    SELECT
        scenario,
        measured_impressions,
        cpm_micros,
        expected_verification_cost_micros
    FROM {{ ref('epic41_verification_fixture') }}
),

twin_rows AS (
    SELECT
        breakdown_value,
        measured_impressions,
        verification_cost_micros
    FROM {{ ref('fee_tax_verification_allocation') }}
    WHERE project_id = 'feetax_dev_verif_twin_b'
      AND row_kind   = 'allocation'
      AND date       = CAST('2026-05-01' AS DATE)
),

cardinality_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: epic41_verification_fixture is empty or'
        || ' feetax_dev_verif_twin_b produced no allocation row -- run'
        || ' dbt/seeds/feetax/seed_fee_tax_verification.py, then rebuild fact_daily_kpi'
        || ' BEFORE the overlay (the stale-mart trap).' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM fixture) = 0
       OR (SELECT COUNT(*) FROM twin_rows) <> 2
),

anti_vacuity_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'ANTI_VACUITY_FAIL: fewer than 2 allocation rows carry a POSITIVE'
        || ' verification_cost_micros, so the pinned split below is trivially satisfied'
            AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (
        SELECT COUNT(*)
        FROM {{ ref('fee_tax_verification_allocation') }}
        WHERE row_kind = 'allocation'
          AND verification_cost_micros > 0
    ) < 2
),

-- ------------------------------------------------- HALF 1: REPLAY THE MACRO --
macro_breaks AS (
    SELECT
        f.scenario AS subject,
        'CPM_MACRO_FAIL: fee_tax_cpm_micros('
            || CAST(f.measured_impressions AS STRING) || ', '
            || CAST(f.cpm_micros AS STRING) || ') gave '
            || COALESCE(CAST({{ fee_tax_cpm_micros('f.measured_impressions', 'f.cpm_micros') }} AS STRING), 'NULL')
            || ' but the pinned value is '
            || COALESCE(CAST(f.expected_verification_cost_micros AS STRING), 'NULL')
            AS failure_reason
    FROM fixture f
    WHERE
        -- NULL-safe: an expected NULL (a fail-closed guard) must produce a NULL, and a
        -- pinned integer must produce exactly that integer.
        (({{ fee_tax_cpm_micros('f.measured_impressions', 'f.cpm_micros') }}) IS NULL)
            <> (f.expected_verification_cost_micros IS NULL)
        OR (
            ({{ fee_tax_cpm_micros('f.measured_impressions', 'f.cpm_micros') }}) IS NOT NULL
            AND f.expected_verification_cost_micros IS NOT NULL
            AND ({{ fee_tax_cpm_micros('f.measured_impressions', 'f.cpm_micros') }})
                <> f.expected_verification_cost_micros
        )
),

-- ------------------------------------------------- HALF 2: THE LIVE MODEL ----
pinned_breaks AS (
    SELECT
        'feetax_dev_verif_twin_b|' || t.breakdown_value AS subject,
        'ALLOCATION_PINNED_FAIL: ' || t.breakdown_value || ' priced '
            || COALESCE(CAST(t.verification_cost_micros AS STRING), 'NULL')
            || ' micros; the pinned figure is '
            || CASE t.breakdown_value WHEN 'camp_x' THEN '3086417500' ELSE '1913582500' END
            AS failure_reason
    FROM twin_rows t
    WHERE t.verification_cost_micros IS DISTINCT FROM
          CASE t.breakdown_value
              WHEN 'camp_x' THEN 3086417500
              WHEN 'camp_y' THEN 1913582500
          END
),

residue_break AS (
    SELECT
        'feetax_dev_verif_twin_b|2026-05-01' AS subject,
        'ALLOCATION_RESIDUE_FAIL: the day total is '
            || COALESCE(CAST(SUM(t.verification_cost_micros) AS STRING), 'NULL')
            || ' micros, expected EXACTLY 5000000000 -- the per-campaign parts must sum to'
            || ' the whole with no remainder' AS failure_reason
    FROM twin_rows t
    HAVING COALESCE(SUM(t.verification_cost_micros), -1) <> 5000000000
),

proportionality_break AS (
    -- Cross-multiplied, so no division and no float step.
    SELECT
        a.breakdown_value || ' vs ' || b.breakdown_value AS subject,
        'ALLOCATION_PROPORTION_FAIL: the costs are not in the same ratio as the measured'
        || ' impressions -- ' || CAST(a.verification_cost_micros AS STRING) || '/'
        || CAST(a.measured_impressions AS STRING) || ' vs '
        || CAST(b.verification_cost_micros AS STRING) || '/'
        || CAST(b.measured_impressions AS STRING) AS failure_reason
    FROM twin_rows a
    JOIN twin_rows b
        ON a.breakdown_value < b.breakdown_value
    WHERE a.verification_cost_micros * b.measured_impressions
       <> b.verification_cost_micros * a.measured_impressions
)

SELECT subject, failure_reason FROM cardinality_guard
UNION ALL SELECT subject, failure_reason FROM anti_vacuity_guard
UNION ALL SELECT subject, failure_reason FROM macro_breaks
UNION ALL SELECT subject, failure_reason FROM pinned_breaks
UNION ALL SELECT subject, failure_reason FROM residue_break
UNION ALL SELECT subject, failure_reason FROM proportionality_break
{%- endif -%}
