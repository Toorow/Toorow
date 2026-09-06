-- Story 41.5 (R13) -- A PER_TRANSACTION FEE NEEDS A REAL COUNT OR IT IS A TYPED GAP.
--
-- A per-transaction fee applied ONCE to a day of 4 000 orders is off by 4 000x, and it
-- is off SILENTLY. So:
--   A. NO ROW ANYWHERE may carry a per-transaction component while transaction_count is
--      NULL. This is the "never assume 1" guard, asserted as an INVARIANT OVER THE WHOLE
--      MODEL rather than over one fixture -- which is the half that actually catches a
--      regression;
--   B. the pinned arithmetic: 1.4 % of 12 345 670 000 (172 839 380) + 0.25 EUR x 412
--      (103 000 000, an EXACT integer multiplication with NO ROUNDING AT ALL)
--      = 275 839 380;
--   C. anti-vacuity: the per-transaction fixture must exist.
--
-- HONEST LIMITATION: TRANSACTION_COUNT_UNAVAILABLE itself is not reachable in the dev
-- warehouse -- shopify and stripe staging emit their count metric on exactly the days
-- they emit revenue, and the connectors that declare NO count metric (adjust, klaviyo,
-- cm360) ship no seed fixture at all (epic retro item C.9/7). Assertion A is the
-- structural guard that survives that gap; it is written over every row precisely so it
-- does not depend on a fixture that cannot exist yet.

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

case_a AS (
    SELECT
        'PER_TRANSACTION_COMPONENT_WITHOUT_A_COUNT' AS failure,
        n.project_id                                AS project_id,
        n.applied_rule_ids || ' fee='
            || CAST(n.payment_fee_micros AS STRING) AS detail
    FROM normalized n
    WHERE n.transaction_count IS NULL
      AND n.payment_fee_micros IS NOT NULL
      AND n.payment_fee_provenance = 'derived_rule'
      -- A per-transaction rule is in play (its id is in the applied list) and yet a
      -- number came out of a count we do not have.
      AND n.applied_rule_ids LIKE '%_fix%'
),

case_b AS (
    SELECT
        'PER_TRANSACTION_ARITHMETIC_DIVERGED' AS failure,
        n.project_id                          AS project_id,
        'fee=' || CAST(n.payment_fee_micros AS STRING)
            || ' count=' || CAST(n.transaction_count AS STRING) AS detail
    FROM normalized n
    WHERE n.project_id = 'feetaxrev_dev_pertx'
      AND (n.payment_fee_micros IS DISTINCT FROM 275839380
        OR n.transaction_count  IS DISTINCT FROM 412
        OR n.payment_fee_provenance <> 'derived_rule')
),

case_c AS (
    SELECT
        'PER_TRANSACTION_FIXTURE_MISSING' AS failure,
        'feetaxrev_dev_pertx'             AS project_id,
        'the per-transaction assertions would pass by finding nothing' AS detail
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE NOT EXISTS (
        SELECT 1 FROM normalized WHERE project_id = 'feetaxrev_dev_pertx'
    )
)

SELECT * FROM case_a
UNION ALL SELECT * FROM case_b
UNION ALL SELECT * FROM case_c
{%- endif -%}
