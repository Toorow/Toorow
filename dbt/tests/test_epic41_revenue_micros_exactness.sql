-- Story 41.5 AC6 (C.8/6) -- MICROS ARE NORMALISED PER SOURCE ROW, THEN SUMMED EXACTLY.
--
-- `ROUND(SUM(value) * 1e6)` would put the single rounding boundary AFTER the float drift
-- instead of before it, and the difference only shows up on values that are not exactly
-- representable -- i.e. on real money, later, in a customer's invoice.
--
--   A. every micros column is a whole integer (no fractional residue survived);
--   B. the landed figure equals the PER-SOURCE-ROW normalisation of the underlying fact
--      rows, computed here with the SAME macro the model uses -- so a divergence is a
--      real one and not an artefact of a second normalisation written in the test;
--   C. the half-micro fixture is present, because it is the one value that can
--      distinguish the two orderings at all.

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
    SELECT 'MICROS_COLUMN_IS_NOT_A_WHOLE_INTEGER' AS failure, project_id,
           connector || '/' || metric AS detail
    FROM normalized
    WHERE landed_revenue_micros <> CAST(landed_revenue_micros AS BIGINT)
       OR COALESCE(gross_revenue_ttc_micros, 0) <> CAST(COALESCE(gross_revenue_ttc_micros, 0) AS BIGINT)
       OR COALESCE(net_revenue_ht_micros, 0)    <> CAST(COALESCE(net_revenue_ht_micros, 0) AS BIGINT)
       OR COALESCE(sales_tax_micros, 0)         <> CAST(COALESCE(sales_tax_micros, 0) AS BIGINT)
       OR COALESCE(payment_fee_micros, 0)       <> CAST(COALESCE(payment_fee_micros, 0) AS BIGINT)
),

per_source_row AS (
    SELECT
        f.project_id,
        CAST(f.date AS DATE)                    AS date,
        f.connector,
        f.metric,
        f.breakdown_dimension,
        SUM({{ fee_tax_to_micros('f.value') }}) AS micros
    FROM {{ ref('fact_daily_kpi') }} f
    JOIN {{ ref('fee_tax_revenue_scope') }} s
        ON  s.connector        = f.connector
        AND s.canonical_metric = f.metric
    GROUP BY f.project_id, CAST(f.date AS DATE), f.connector, f.metric,
             f.breakdown_dimension
),

case_b AS (
    SELECT 'LANDED_MICROS_DIVERGED_FROM_PER_SOURCE_ROW_NORMALISATION' AS failure,
           n.project_id AS project_id,
           n.connector || '/' || n.metric || ' model='
               || CAST(n.landed_revenue_micros AS STRING)
               || ' recomputed=' || CAST(p.micros AS STRING) AS detail
    FROM normalized n
    JOIN per_source_row p
        ON  p.project_id = n.project_id
        AND p.date       = n.date
        AND p.connector  = n.connector
        AND p.metric     = n.metric
    -- Only single-series sources can be compared 1:1 here; a multi-series source is the
    -- subject of test_epic41_revenue_no_double_count_single_breakdown.sql instead.
    WHERE 1 = (
        SELECT COUNT(*) FROM per_source_row q
        WHERE q.project_id = n.project_id AND q.date = n.date
          AND q.connector = n.connector AND q.metric = n.metric
    )
      AND n.landed_revenue_micros <> p.micros
),

case_c AS (
    SELECT 'HALF_MICRO_FIXTURE_MISSING' AS failure, 'feetaxrev_dev_half' AS project_id,
           'the rounding-order assertion has no discriminating value to work on'
               AS detail
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE NOT EXISTS (
        SELECT 1 FROM normalized
        WHERE project_id = 'feetaxrev_dev_half'
          AND landed_revenue_micros = 12000000003
    )
)

SELECT * FROM case_a
UNION ALL SELECT * FROM case_b
UNION ALL SELECT * FROM case_c
{%- endif -%}
