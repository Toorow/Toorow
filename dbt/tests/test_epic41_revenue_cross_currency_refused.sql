-- Story 41.5 (E41-FR05) -- A FOREIGN-CURRENCY AMOUNT IS REFUSED, NOT CONVERTED.
--
-- The only place two currencies can meet on the revenue side is a RULE-DECLARED absolute
-- amount: FLAT.amount_micros and PER_TRANSACTION.amount_micros. Converting it here would
-- apply FX A SECOND TIME outside the fx_convert_at_read locus (Epic 39.10), so the
-- component is REFUSED -- not summed, NOT CONVERTED -- and the row says so.
-- PERCENTAGE rates are DIMENSIONLESS and are never subject to the check, which is the
-- half that stops the guard from being satisfied by refusing everything.
--
--   A. the USD-on-EUR fixture gaps with CURRENCY_MISMATCH and its fee is NULL;
--   B. it was NOT converted: no non-NULL payment fee on a currency-mismatched row;
--   C. the VAT PERCENTAGE rule on the SAME row still fires -- the refusal is scoped to
--      the absolute amount, not to the whole row;
--   D. anti-vacuity.

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

refused AS (
    SELECT * FROM normalized WHERE project_id = 'feetaxrev_dev_refused'
),

case_a AS (
    SELECT 'CROSS_CURRENCY_NOT_REFUSED' AS failure, project_id, gap_codes AS detail
    FROM refused WHERE NOT gap_currency_mismatch
),

case_b AS (
    SELECT 'CROSS_CURRENCY_AMOUNT_WAS_SUMMED_OR_CONVERTED' AS failure, project_id,
           CAST(payment_fee_micros AS STRING) AS detail
    FROM normalized
    WHERE gap_currency_mismatch
      AND payment_fee_micros IS NOT NULL
),

case_c AS (
    SELECT 'DIMENSIONLESS_RATE_WAS_REFUSED_TOO' AS failure, project_id,
           COALESCE(CAST(net_revenue_ht_micros AS STRING), 'NULL') AS detail
    FROM refused
    -- 1 000 000 000 TTC at 20 % -> 833 333 333 HT. The VAT rule must be untouched by the
    -- payment-fee refusal.
    WHERE net_revenue_ht_micros IS DISTINCT FROM 833333333
),

case_d AS (
    SELECT 'CROSS_CURRENCY_FIXTURE_MISSING' AS failure, 'feetaxrev_dev_refused' AS project_id,
           'the refusal assertions would pass by finding nothing' AS detail
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE NOT EXISTS (SELECT 1 FROM refused)
)

SELECT * FROM case_a
UNION ALL SELECT * FROM case_b
UNION ALL SELECT * FROM case_c
UNION ALL SELECT * FROM case_d
{%- endif -%}
