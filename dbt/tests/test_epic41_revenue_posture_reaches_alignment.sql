-- Story 48.4, completeness criterion [4] -- "revenue and spend use incompatible
-- net/gross postures WITHOUT WARNING".
--
-- The homogeneity test beside this one proves a mixed pair is UNCONSTRUCTIBLE. That is
-- the refusal. This test is about the WARNING: when the comparison is refused because a
-- source never declared whether its landed figure includes tax, the surface where
-- revenue meets spend must say SO, and not merely that something upstream was short.
--
-- Two defects it pins, both found by reading the built model on 2026-08-04:
--
--   1. `REVENUE_TAX_POSTURE_UNDECLARED` existed in fee_tax_revenue_normalized_daily and
--      reached the alignment view as NOTHING. A day whose revenue posture was unknown
--      was reported with the same generic REVENUE_NORMALIZATION_INCOMPLETE as a day
--      whose VAT rule was missing -- two different questions, one answer, and only one
--      of them is answerable by the operator.
--   2. `MIN(gap_codes)` over the winning connector's rows. The empty string is the
--      SMALLEST string, so one clean row among gapped siblings reported `''` -- a clean
--      bill of health over a short row. It is MAX now, which returns `''` only when
--      every contributing row is clean.
--
-- EVERY CASE HERE RUNS AGAINST REAL ROWS. That was not true when this test was written
-- a few hours earlier, and the correction is the interesting part: cases B, C and D were
-- STRUCTURAL because `adjust` is the only connector in fee_tax_revenue_scope.csv
-- carrying `UNDECLARED` and was believed to ship no DuckDB seed loader. It ships one
-- (`server/modules/adjust/seeds/load_adjust_seed.py`, Story 53.10) -- the 41.5 seeder's
-- "cannot reach" note had gone stale. Wiring it in gave `feetaxrev_dev_posture`, and
-- with it 4 alignment rows carrying an undeclared posture, none with a figure, none with
-- a ratio, none reported complete. Measured 2026-08-04:
--
--     case A population   14 rows     (a revenue side that exists and is incomplete)
--     cases B/C/D          4 rows     (an undeclared posture reaching alignment)
--
-- Completeness criterion [4] of tax-fees closes on THIS, not on the earlier version of
-- this file: a refusal nobody can falsify is an assertion.

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

-- A. A REVENUE SIDE THAT EXISTS AND IS INCOMPLETE MUST SAY WHY.
-- Scoped to rows that HAVE a revenue side: when there is none, `REVENUE_SIDE_ABSENT` is
-- itself the reason and there are no upstream gap codes to carry.
case_a AS (
    SELECT 'INCOMPLETE_REVENUE_WITH_NO_REASON' AS failure,
           a.project_id                        AS project_id,
           a.revenue_source || ' ' || CAST(a.date AS STRING) AS detail
    FROM alignment a
    WHERE a.revenue_source IS NOT NULL
      AND a.is_revenue_complete = FALSE
      AND a.revenue_gap_codes = ''
),

-- B. THE TYPED COLUMN AND THE STRING MUST AGREE, IN BOTH DIRECTIONS.
-- A surface reads one or the other; two ways of saying the same thing that can drift
-- apart is how a warning goes missing on exactly one of them.
case_b AS (
    SELECT 'POSTURE_FLAG_AND_GAP_CODE_DISAGREE' AS failure,
           a.project_id                         AS project_id,
           'flag=' || CAST(a.is_revenue_posture_undeclared AS STRING)
                   || ' codes=' || a.gap_codes  AS detail
    FROM alignment a
    WHERE a.is_revenue_posture_undeclared
       <> (POSITION('REVENUE_TAX_POSTURE_UNDECLARED' IN a.gap_codes) > 0)
),

-- C. AN UNDECLARED POSTURE MAY NEVER PRODUCE A RATIO.
-- AC7: "Incompatible posture is a visible warning/refusal, NEVER A PLAUSIBLE ROAS." A
-- number computed from a figure whose tax basis nobody knows is the most dangerous
-- output this model could emit, because it looks exactly like a correct one.
case_c AS (
    SELECT 'RATIO_ON_AN_UNDECLARED_POSTURE' AS failure,
           a.project_id                     AS project_id,
           a.ratio_kind || '=' || CAST(a.revenue_cost_ratio AS STRING) AS detail
    FROM alignment a
    WHERE a.is_revenue_posture_undeclared
      AND a.revenue_cost_ratio IS NOT NULL
),

-- D. AND IT MAY NEVER BE CALLED COMPLETE.
case_d AS (
    SELECT 'UNDECLARED_POSTURE_REPORTED_COMPLETE' AS failure,
           a.project_id                           AS project_id,
           a.gap_codes                            AS detail
    FROM alignment a
    WHERE a.is_revenue_posture_undeclared
      AND a.is_alignment_complete
),

-- The anti-vacuity guard case A depends on: if no alignment row has a revenue side at
-- all, case A passes by finding nothing and proves nothing.
vacuity AS (
    SELECT 'NO_REVENUE_SIDE_IN_ANY_ALIGNMENT_ROW' AS failure,
           'n/a'                                  AS project_id,
           'case A would pass by finding nothing' AS detail
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE NOT EXISTS (SELECT 1 FROM alignment WHERE revenue_source IS NOT NULL)
)

SELECT * FROM case_a
UNION ALL SELECT * FROM case_b
UNION ALL SELECT * FROM case_c
UNION ALL SELECT * FROM case_d
UNION ALL SELECT * FROM vacuity
{%- endif -%}
