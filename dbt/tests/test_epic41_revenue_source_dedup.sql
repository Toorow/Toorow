-- Story 41.5 (R7) -- ONE WINNING REVENUE SOURCE PER DAY.
--
-- A Stripe payment settling a Shopify order measures the SAME SALE. Summing the two
-- would double-count it, and the number would look entirely plausible.
--
--   A. exactly ONE revenue_source per (project, date, currency);
--   B. THE DISCRIMINATING HALF -- on a project-day where TWO revenue connectors landed,
--      the alignment TTC revenue must be STRICTLY LESS than their sum. An implementation
--      that summed both would pass A (it could still report one source name) and fail
--      here, which is exactly why A alone is not enough.
--   C. the winner is the connector with the LOWEST declared priority present that day,
--      so the seed actually drives the pick rather than an accident of ordering.

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

sales AS (
    SELECT *
    FROM {{ ref('fee_tax_revenue_normalized_daily') }}
    WHERE revenue_role = 'SALES'
),

case_a AS (
    SELECT
        'MORE_THAN_ONE_REVENUE_SOURCE' AS failure,
        a.project_id                   AS project_id,
        CAST(a.date AS STRING) || ' sources='
            || CAST(COUNT(DISTINCT a.revenue_source) AS STRING) AS detail
    FROM alignment a
    WHERE a.revenue_source IS NOT NULL
    GROUP BY a.project_id, a.date, a.currency
    HAVING COUNT(DISTINCT a.revenue_source) > 1
),

multi_source_days AS (
    SELECT
        s.project_id, s.date, s.currency,
        COUNT(DISTINCT s.connector)               AS connector_count,
        SUM(s.gross_revenue_ttc_micros)           AS all_sources_ttc_micros
    FROM sales s
    GROUP BY s.project_id, s.date, s.currency
    HAVING COUNT(DISTINCT s.connector) > 1
),

case_b AS (
    SELECT
        'ALIGNMENT_SUMMED_TWO_REVENUE_SOURCES' AS failure,
        m.project_id                           AS project_id,
        'aligned=' || CAST(a.revenue_micros AS STRING)
            || ' all_sources=' || CAST(m.all_sources_ttc_micros AS STRING) AS detail
    FROM multi_source_days m
    JOIN alignment a
        ON  a.project_id = m.project_id
        AND a.date       = m.date
        AND a.currency   = m.currency
        AND a.tax_basis  = 'TTC'
    WHERE a.revenue_micros IS NOT NULL
      AND a.revenue_micros >= m.all_sources_ttc_micros
),

-- The anti-vacuity guard for B: the whole point of the fixture is that such a day
-- EXISTS. If none does, B proves nothing and that is a failure of the fixture.
case_b_vacuous AS (
    SELECT
        'NO_MULTI_SOURCE_DAY_IN_FIXTURE' AS failure,
        'n/a'                            AS project_id,
        'the strictly-less assertion would pass by finding nothing' AS detail
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE NOT EXISTS (SELECT 1 FROM multi_source_days)
),

declared_priority AS (
    SELECT
        s.project_id, s.date, s.currency,
        MIN(COALESCE(p.priority, 99)) AS best_priority
    FROM sales s
    LEFT JOIN {{ ref('metric_source_priority') }} p
        ON  p.metric    = s.metric
        AND p.connector = s.connector
    GROUP BY s.project_id, s.date, s.currency
),

case_c AS (
    SELECT
        'WINNER_IS_NOT_THE_BEST_DECLARED_PRIORITY' AS failure,
        a.project_id                               AS project_id,
        a.revenue_source || ' at ' || CAST(a.revenue_source_priority AS STRING)
            || ' but best available is ' || CAST(d.best_priority AS STRING) AS detail
    FROM alignment a
    JOIN declared_priority d
        ON  d.project_id = a.project_id
        AND d.date       = a.date
        AND d.currency   = a.currency
    WHERE a.revenue_source IS NOT NULL
      AND a.revenue_source_priority <> d.best_priority
)

SELECT * FROM case_a
UNION ALL SELECT * FROM case_b
UNION ALL SELECT * FROM case_b_vacuous
UNION ALL SELECT * FROM case_c
{%- endif -%}
