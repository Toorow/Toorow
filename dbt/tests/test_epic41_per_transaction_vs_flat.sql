-- Story 41.5 (R13 || R13b, section B.4bis) -- THE TWO FORMS ARE TWO BEHAVIOURS.
--
-- This is the test that makes the PER_TRANSACTION vocabulary change REAL rather than
-- cosmetic. `feetaxrev_dev_pertx` and `feetaxrev_dev_flatfee` are a CONTROLLED PAIR:
-- same revenue (12 345.67 EUR), same order count (412), same 1.4 % percentage rule, same
-- declared amount (0.25 EUR = 250 000 micros). THE FORM OF THE SECOND RULE IS THE ONLY
-- DIFFERENCE:
--
--   PER_TRANSACTION -> 250 000 x 412 = 103 000 000 micros (it SCALES with the count)
--   FLAT            -> 250 000 micros, ONCE for the row  (it does NOT)
--
-- so the totals are 172 839 380 + 103 000 000 = 275 839 380 against
-- 172 839 380 + 250 000 = 173 089 380.
--
-- Without this pair a regression that silently re-merged the two behaviours -- treating
-- PER_TRANSACTION as an alias of FLAT, or vice versa -- would pass every other test in
-- the suite, because each form is individually plausible on its own fixture.
--
--   A. the two projects must produce DIFFERENT payment fees;
--   B. each must produce its OWN pinned value;
--   C. both fixtures must exist, or A and B prove nothing.

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
WITH normalized AS (
    SELECT * FROM {{ ref('fee_tax_revenue_normalized_daily') }}
),

per_tx AS (
    SELECT * FROM normalized WHERE project_id = 'feetaxrev_dev_pertx'
),

flat AS (
    SELECT * FROM normalized WHERE project_id = 'feetaxrev_dev_flatfee'
),

case_a AS (
    SELECT
        'PER_TRANSACTION_AND_FLAT_PRODUCED_THE_SAME_FEE' AS failure,
        'feetaxrev_dev_pertx|feetaxrev_dev_flatfee'      AS project_id,
        CAST(p.payment_fee_micros AS STRING) || ' vs '
            || CAST(f.payment_fee_micros AS STRING)      AS detail
    FROM per_tx p
    CROSS JOIN flat f
    WHERE p.payment_fee_micros = f.payment_fee_micros
),

case_b AS (
    SELECT
        'PINNED_FORM_VALUE_DIVERGED' AS failure,
        n.project_id                 AS project_id,
        CAST(n.payment_fee_micros AS STRING) AS detail
    FROM normalized n
    WHERE (n.project_id = 'feetaxrev_dev_pertx'
           -- 1.4 % (172 839 380) + 0.25 EUR x 412 (103 000 000)
           AND n.payment_fee_micros IS DISTINCT FROM 275839380)
       OR (n.project_id = 'feetaxrev_dev_flatfee'
           -- 1.4 % (172 839 380) + 0.25 EUR ONCE (250 000). The count is irrelevant to
           -- a FLAT amount, which is the whole point of the comparison.
           AND n.payment_fee_micros IS DISTINCT FROM 173089380)
),

case_c AS (
    SELECT
        'FORM_COMPARISON_FIXTURE_MISSING' AS failure,
        'feetaxrev_dev_pertx|feetaxrev_dev_flatfee' AS project_id,
        'the two-forms comparison would pass by finding nothing' AS detail
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE NOT EXISTS (SELECT 1 FROM per_tx)
       OR NOT EXISTS (SELECT 1 FROM flat)
)

SELECT * FROM case_a
UNION ALL SELECT * FROM case_b
UNION ALL SELECT * FROM case_c
{%- endif -%}
