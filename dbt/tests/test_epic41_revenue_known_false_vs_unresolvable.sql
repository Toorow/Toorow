-- Story 41.5 (R10 || R11, E41-AD9) -- KNOWN-FALSE AND UNRESOLVABLE ARE DIFFERENT THINGS.
--
-- The distinction is the epic's central honesty rule, and it is asserted in BOTH
-- DIRECTIONS because a one-sided assertion is satisfiable by an implementation that
-- gaps everything (or gaps nothing).
--
--   KNOWN FALSE  (feetaxrev_dev_knownfalse): a VAT rule conditioned country='DE' on a row
--                whose country RESOLVES to FR. The rule contributes +0 and raises NO
--                gap; a second, unconditioned VAT rule fires, so the row is COMPLETE and
--                its HT view is populated at 20 %.
--   UNRESOLVABLE (feetaxrev_dev_srctype): a VAT rule scoped source_type=COMMERCE_REVENUE
--                on a row whose attr_source_type is NULL, because NOTHING WRITES
--                app.datastream_source_types yet. NULL is UNRESOLVED, NEVER known-false.
--                Reading it as known-false would compute NOTHING while reporting a
--                complete, trustworthy total -- the worst available failure mode, and
--                the reason this fixture exists at all.

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

known_false AS (
    SELECT * FROM normalized WHERE project_id = 'feetaxrev_dev_knownfalse'
),

unresolvable AS (
    SELECT * FROM normalized WHERE project_id = 'feetaxrev_dev_srctype'
),

-- Half 1: known-false must NOT gap, and the row must compose normally.
case_known_false AS (
    SELECT 'KNOWN_FALSE_RAISED_A_GAP' AS failure, project_id, gap_codes AS detail
    FROM known_false
    WHERE gap_codes <> ''
    UNION ALL
    SELECT 'KNOWN_FALSE_ROW_DID_NOT_COMPOSE' AS failure, project_id,
           COALESCE(CAST(net_revenue_ht_micros AS STRING), 'NULL') AS detail
    FROM known_false
    WHERE net_revenue_ht_micros IS NULL
    UNION ALL
    -- The DE rule must not have fired: at 19 % the net of 1 000 000 000 would be
    -- 840 336 134, not the 833 333 333 the unconditioned 20 % rule gives.
    SELECT 'KNOWN_FALSE_RULE_ACTUALLY_FIRED' AS failure, project_id,
           CAST(net_revenue_ht_micros AS STRING) AS detail
    FROM known_false
    WHERE net_revenue_ht_micros IS DISTINCT FROM 833333333
),

-- Half 2: unresolvable MUST gap, and must gap with the RIGHT code.
case_unresolvable AS (
    SELECT 'UNRESOLVABLE_SOURCE_TYPE_DID_NOT_GAP' AS failure, project_id,
           gap_codes AS detail
    FROM unresolvable
    WHERE NOT gap_source_type_unresolved
    UNION ALL
    -- ... and the rule must NOT have fired, i.e. HT must be NULL. If it had been read as
    -- known-false the row would have composed silently with no VAT at all.
    SELECT 'UNRESOLVABLE_RULE_FIRED_ANYWAY' AS failure, project_id,
           CAST(net_revenue_ht_micros AS STRING) AS detail
    FROM unresolvable
    WHERE net_revenue_ht_micros IS NOT NULL
),

vacuity AS (
    SELECT 'KNOWN_FALSE_FIXTURE_MISSING' AS failure, 'feetaxrev_dev_knownfalse' AS project_id,
           'both halves of E41-AD9 must be present or neither proves anything' AS detail
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE NOT EXISTS (SELECT 1 FROM known_false)
    UNION ALL
    SELECT 'UNRESOLVABLE_FIXTURE_MISSING', 'feetaxrev_dev_srctype',
           'both halves of E41-AD9 must be present or neither proves anything'
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE NOT EXISTS (SELECT 1 FROM unresolvable)
)

SELECT * FROM case_known_false
UNION ALL SELECT * FROM case_unresolvable
UNION ALL SELECT * FROM vacuity
{%- endif -%}
