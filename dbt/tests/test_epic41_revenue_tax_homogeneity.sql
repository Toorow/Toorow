-- Story 41.5 AC2 -- TAX HOMOGENEITY IS STRUCTURAL.
--
-- This test does not check that a filter is in place; it TRIES TO CONSTRUCT THE
-- FORBIDDEN PAIR and asserts there is none. Three ways it could exist:
--   A. a row whose tax_basis is neither HT nor TTC (an UNRESOLVED basis leaking in);
--   B. an alignment row whose revenue basis disagrees with the normalized row it came
--      from -- i.e. HT revenue sitting on a TTC row or vice versa;
--   C. an alignment row backed by an UNRESOLVED-basis SALES row that carries a figure,
--      a ratio, or no stated reason -- i.e. an untyped basis that was COMPARED rather
--      than refused. (This case asserted mere ABSENCE until 2026-08-04; see its own
--      comment below for why absence was the wrong assertion and refusal is stronger.)
-- Plus the anti-vacuity guard: if no alignment row exists at all, A/B/C pass by finding
-- nothing, so emptiness is itself reported.

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

normalized AS (
    SELECT * FROM {{ ref('fee_tax_revenue_normalized_daily') }}
),

case_a AS (
    SELECT 'UNTYPED_BASIS_IN_ALIGNMENT' AS failure,
           a.project_id AS project_id,
           a.tax_basis  AS detail
    FROM alignment a
    WHERE a.tax_basis NOT IN ('HT', 'TTC')
),

-- The HT row's revenue must be the winner's NET HT figure and the TTC row's must be its
-- GROSS TTC figure. If the two were ever crossed, this finds it.
case_b AS (
    SELECT 'REVENUE_BASIS_CROSSED' AS failure,
           a.project_id            AS project_id,
           a.tax_basis || ' revenue=' || CAST(a.revenue_micros AS STRING) AS detail
    FROM alignment a
    JOIN normalized n
        ON  n.project_id = a.project_id
        AND n.date       = a.date
        AND n.currency   = a.currency
        AND n.connector  = a.revenue_source
        AND n.revenue_role = 'SALES'
    WHERE a.revenue_micros IS NOT NULL
      AND (
            (a.tax_basis = 'HT'  AND n.net_revenue_ht_micros    IS NULL)
         OR (a.tax_basis = 'TTC' AND n.gross_revenue_ttc_micros IS NULL)
          )
),

-- C. AN UNRESOLVED BASIS MAY REACH ALIGNMENT ONLY AS A REFUSAL.
--
-- ⚠️ THIS CASE USED TO ASSERT THE OPPOSITE: that no alignment row may exist at all for
-- a project-day whose normalized row has an UNRESOLVED basis. It asserted an intent the
-- model deliberately does not implement, and no fixture could reach the case, so it
-- passed for eight days by finding nothing. The day `adjust` became seedable
-- (2026-08-04) it failed with 6 rows -- against CORRECT behaviour.
--
-- Suppressing the row would report "this project has no revenue" for a project that has
-- revenue whose tax basis nobody stated. That silence is what completeness criterion [4]
-- of tax-fees forbids, so the row must EXIST and must REFUSE. The assertion is therefore
-- the refusal itself, which is strictly stronger than absence: no money, no ratio, and
-- the posture named. A regression that started comparing an unresolved figure now fails
-- here, where before it would have been reported as an absent revenue side.
case_c AS (
    SELECT 'UNRESOLVED_BASIS_COMPARED_INSTEAD_OF_REFUSED' AS failure,
           a.project_id AS project_id,
           a.revenue_source || ' ' || a.tax_basis
             || ' revenue=' || COALESCE(CAST(a.revenue_micros AS STRING), 'NULL')
             || ' ratio='   || COALESCE(CAST(a.revenue_cost_ratio AS STRING), 'NULL')
             AS detail
    FROM alignment a
    WHERE EXISTS (
              SELECT 1 FROM normalized n
              WHERE n.project_id     = a.project_id
                AND n.date           = a.date
                AND n.connector      = a.revenue_source
                AND n.revenue_role   = 'SALES'
                AND n.landed_basis   = 'UNRESOLVED'
          )
      AND (
              a.revenue_micros IS NOT NULL          -- a figure of unknown basis compared
           OR a.revenue_cost_ratio IS NOT NULL      -- ... or worse, divided
           OR NOT a.is_revenue_posture_undeclared   -- refused without saying why
          )
),

vacuity AS (
    SELECT 'NO_ALIGNMENT_ROWS_AT_ALL' AS failure,
           'n/a' AS project_id,
           'the homogeneity assertions would pass by finding nothing' AS detail
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE NOT EXISTS (SELECT 1 FROM alignment)
)

SELECT * FROM case_a
UNION ALL SELECT * FROM case_b
UNION ALL SELECT * FROM case_c
UNION ALL SELECT * FROM vacuity
{%- endif -%}
