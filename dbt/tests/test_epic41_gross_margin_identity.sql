-- Story 41.5 AC3 -- THE RATIO AND THE MARGIN ARE EXACT, AND THE HT ROW IS THE ONLY ONE
-- THAT CARRIES A MARGIN.
--
--   A. gross_margin_micros = revenue_micros - cost_micros EXACTLY on every HT row where
--      both sides exist (integer subtraction, no rounding, no division);
--   B. gross_margin_micros is NULL on EVERY TTC row -- a "margin" computed from money in
--      including the customer's VAT minus money out including the agency's VAT is not a
--      margin, it is two different taxes on two different flows;
--   C. ratio_kind matches the basis, so a cash ratio can never be read as performance;
--   D. THE FLATTERING-LIE GUARD: when the cost ladder is incomplete, cost_micros and
--      revenue_cost_ratio are BOTH NULL. Summing only the complete cost rows would
--      understate cost and therefore OVERSTATE ROAS -- silently, and in the direction
--      everyone wants to believe;
--   E. the ratio equals revenue/cost where both exist, and is NULL (never infinity) at
--      zero cost;
--   F. anti-vacuity: a complete HT row with a real ratio must exist, or A/C/E are about
--      an empty set.

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
WITH alignment AS (
    SELECT * FROM {{ ref('fee_tax_revenue_alignment_daily') }}
),

case_a AS (
    SELECT 'GROSS_MARGIN_IS_NOT_AN_EXACT_SUBTRACTION' AS failure, project_id,
           CAST(gross_margin_micros AS STRING) AS detail
    FROM alignment
    WHERE tax_basis = 'HT'
      AND revenue_micros IS NOT NULL
      AND cost_micros IS NOT NULL
      AND gross_margin_micros IS DISTINCT FROM (revenue_micros - cost_micros)
),

case_b AS (
    SELECT 'TTC_ROW_CARRIES_A_MARGIN' AS failure, project_id,
           CAST(gross_margin_micros AS STRING) AS detail
    FROM alignment
    WHERE tax_basis = 'TTC' AND gross_margin_micros IS NOT NULL
),

case_c AS (
    SELECT 'RATIO_KIND_DOES_NOT_MATCH_THE_BASIS' AS failure, project_id,
           tax_basis || '/' || COALESCE(ratio_kind, 'NULL') AS detail
    FROM alignment
    WHERE (tax_basis = 'HT'  AND ratio_kind <> 'roas_ht')
       OR (tax_basis = 'TTC' AND ratio_kind <> 'cash_cover_ttc')
),

case_d AS (
    SELECT 'INCOMPLETE_LADDER_STILL_PRODUCED_A_COST_OR_A_RATIO' AS failure, project_id,
           'cost=' || COALESCE(CAST(cost_micros AS STRING), 'NULL')
               || ' ratio=' || COALESCE(CAST(revenue_cost_ratio AS STRING), 'NULL')
               || ' gaps=' || gap_codes AS detail
    FROM alignment
    WHERE is_cost_complete = FALSE
      AND cost_row_count > 0
      AND (cost_micros IS NOT NULL OR revenue_cost_ratio IS NOT NULL)
),

case_e AS (
    SELECT 'RATIO_IS_NOT_REVENUE_OVER_COST' AS failure, project_id,
           CAST(revenue_cost_ratio AS STRING) AS detail
    FROM alignment
    WHERE revenue_micros IS NOT NULL
      AND cost_micros IS NOT NULL
      AND cost_micros <> 0
      AND ABS(revenue_cost_ratio
              - (CAST(revenue_micros AS {{ toorow_float_type() }}) / CAST(cost_micros AS {{ toorow_float_type() }}))) > 1e-9
    UNION ALL
    SELECT 'ZERO_COST_DID_NOT_YIELD_NULL', project_id,
           CAST(revenue_cost_ratio AS STRING)
    FROM alignment
    WHERE cost_micros = 0 AND revenue_cost_ratio IS NOT NULL
),

case_f AS (
    SELECT 'NO_COMPLETE_HT_ROAS_ROW_IN_FIXTURE' AS failure, 'n/a' AS project_id,
           'the ratio and margin assertions would pass by finding nothing' AS detail
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE NOT EXISTS (
        SELECT 1 FROM alignment
        WHERE tax_basis = 'HT'
          AND revenue_cost_ratio IS NOT NULL
          AND gross_margin_micros IS NOT NULL
    )
)

SELECT * FROM case_a
UNION ALL SELECT * FROM case_b
UNION ALL SELECT * FROM case_c
UNION ALL SELECT * FROM case_d
UNION ALL SELECT * FROM case_e
UNION ALL SELECT * FROM case_f
{%- endif -%}
