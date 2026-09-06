-- T2 (Story 41.4, AC2 / C.8/6) -- ROUND_HALF_UP, ONE boundary per (row x rule), and the
-- divergence from server/core/money.py's banker's rounding is PINNED rather than
-- discovered later.
--
-- WHY THIS TEST DISCRIMINATES INSTEAD OF DECORATING. Most rounding assertions are
-- vacuous because the two candidate policies agree on the value chosen. This one does
-- not: 999 997 x 2 500 500 / 1000 = 2 500 492 498.5 -- an EXACT half whose integer part
-- (...498) is EVEN. Half-away-from-zero gives 2 500 492 499; half-to-even (Python's
-- round(), which money.py uses) gives 2 500 492 498. So the test asserts BOTH that the
-- engine produces ...499 AND that it does not produce ...498, and a future "let's align
-- the SQL with money.py" change fails LOUDLY here instead of quietly moving invoices.
--
-- Also asserts the ONE LEGITIMATE ZERO: a real measured-zero base is a real zero cost,
-- coverage_state PRICED and is_allocation_complete TRUE -- never a gap, and never
-- confused with the NULL that means "nobody declared a rate".
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
WITH halfup_rows AS (
    SELECT
        breakdown_value,
        measured_impressions,
        verification_cost_micros,
        coverage_state,
        is_allocation_complete
    FROM {{ ref('fee_tax_verification_allocation') }}
    WHERE project_id = 'feetax_dev_verif_halfup'
      AND row_kind   = 'allocation'
),

fixture_halfup AS (
    SELECT measured_impressions, cpm_micros, expected_verification_cost_micros
    FROM {{ ref('epic41_verification_fixture') }}
    WHERE scenario = 'half_up_even'
),

fixture_zero AS (
    SELECT measured_impressions, cpm_micros, expected_verification_cost_micros
    FROM {{ ref('epic41_verification_fixture') }}
    WHERE scenario = 'zero_base'
),

cardinality_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: feetax_dev_verif_halfup produced '
        || CAST((SELECT COUNT(*) FROM halfup_rows) AS STRING)
        || ' allocation rows, expected exactly 1. Run'
        || ' dbt/seeds/feetax/seed_fee_tax_verification.py and rebuild fact_daily_kpi.'
            AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM halfup_rows) <> 1
),

anti_vacuity_guard AS (
    -- Without the fixture row, assertions 1 and 2 below are trivially satisfied.
    SELECT
        CAST(NULL AS STRING) AS subject,
        'ANTI_VACUITY_FAIL: the half_up_even fixture row is missing, so the'
        || ' half-up-vs-bankers discrimination is untested' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM fixture_halfup) <> 1
),

model_half_up_break AS (
    SELECT
        'feetax_dev_verif_halfup|' || h.breakdown_value AS subject,
        'ROUND_HALF_UP_FAIL: the model priced '
            || COALESCE(CAST(h.verification_cost_micros AS STRING), 'NULL')
            || ' micros; ROUND_HALF_UP requires exactly 2500492499' AS failure_reason
    FROM halfup_rows h
    WHERE h.verification_cost_micros IS DISTINCT FROM 2500492499
),

bankers_rounding_detected AS (
    SELECT
        'feetax_dev_verif_halfup|' || h.breakdown_value AS subject,
        'BANKERS_ROUNDING_DETECTED: the model produced 2500492498, which is half-to-EVEN.'
        || ' This engine is ROUND_HALF_UP -- see fee_tax_cpm_micros'' documented,'
        || ' deliberate divergence from server/core/money.py. Do not "fix" the SQL to'
        || ' match Python; the SQL side is the authority (arbitration B2).'
            AS failure_reason
    FROM halfup_rows h
    WHERE h.verification_cost_micros = 2500492498
),

fixture_half_up_break AS (
    SELECT
        'fixture|half_up_even' AS subject,
        'ROUND_HALF_UP_FAIL: the macro replayed to '
            || COALESCE(CAST({{ fee_tax_cpm_micros('f.measured_impressions', 'f.cpm_micros') }} AS STRING), 'NULL')
            || ', expected 2500492499' AS failure_reason
    FROM fixture_halfup f
    WHERE ({{ fee_tax_cpm_micros('f.measured_impressions', 'f.cpm_micros') }})
          IS DISTINCT FROM f.expected_verification_cost_micros
       OR f.expected_verification_cost_micros <> 2500492499
),

-- ------------------------------------------------- THE ONE LEGITIMATE ZERO ---
fixture_zero_break AS (
    SELECT
        'fixture|zero_base' AS subject,
        'ZERO_BASE_FAIL: a real measured-zero base must price EXACTLY 0, not NULL and not'
        || ' a guard refusal; the macro gave '
            || COALESCE(CAST({{ fee_tax_cpm_micros('f.measured_impressions', 'f.cpm_micros') }} AS STRING), 'NULL')
            AS failure_reason
    FROM fixture_zero f
    WHERE ({{ fee_tax_cpm_micros('f.measured_impressions', 'f.cpm_micros') }}) IS DISTINCT FROM 0
),

model_zero_break AS (
    -- Any zero-impression row anywhere in the model: a real zero is not a gap.
    SELECT
        v.project_id || '|' || v.breakdown_value AS subject,
        'ZERO_BASE_FAIL: a zero-impression row must be coverage_state PRICED with'
        || ' verification_cost_micros = 0 and is_allocation_complete TRUE when a rule'
        || ' reaches it; got coverage_state=' || v.coverage_state
        || ' cost=' || COALESCE(CAST(v.verification_cost_micros AS STRING), 'NULL')
            AS failure_reason
    FROM {{ ref('fee_tax_verification_allocation') }} v
    WHERE v.row_kind             = 'allocation'
      AND v.measured_impressions = 0
      AND v.coverage_state       = 'PRICED'
      AND v.gap_codes            = ''
      AND (v.verification_cost_micros IS DISTINCT FROM 0 OR NOT v.is_allocation_complete)
)

SELECT subject, failure_reason FROM cardinality_guard
UNION ALL SELECT subject, failure_reason FROM anti_vacuity_guard
UNION ALL SELECT subject, failure_reason FROM model_half_up_break
UNION ALL SELECT subject, failure_reason FROM bankers_rounding_detected
UNION ALL SELECT subject, failure_reason FROM fixture_half_up_break
UNION ALL SELECT subject, failure_reason FROM fixture_zero_break
UNION ALL SELECT subject, failure_reason FROM model_zero_break
{%- endif -%}
