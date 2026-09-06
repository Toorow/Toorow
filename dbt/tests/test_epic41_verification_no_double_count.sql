-- T7 (Story 41.4, AC8 / C.8/10) -- exactly ONE canonical series per
-- (project_id, date, connector) enters the allocation, so a verification fee can never
-- multiply.
--
-- IT IS A NO-OP TODAY, AND IT SHIPS ANYWAY. `ias` contributes a SINGLE
-- breakdown_dimension ('campaign_id') per metric, so the canonical collapse currently
-- changes nothing. The reason the guard exists is stg_doubleverify_daily: it is
-- LONG-FORMAT, its breakdown_dimension comes straight from the raw feed, and a
-- DoubleVerify mart block could therefore easily land two or three parallel series EACH
-- TOTALLING THE DAY -- at which point every verification fee would double or triple with
-- no error anywhere. This is the same failure the cost ladder's canonical_dim prevents for
-- meta-ads and tiktok-ads, which really do emit three parallel `cost` series today.
--
-- Also asserts the collapse PREFERS 'campaign_id' (rather than MIN(), which would pick a
-- different series alphabetically) and that the collapse does not UNDER-count either: the
-- summed base must equal the fact total of the selected series exactly.
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
WITH module_on AS (
    SELECT project_id
    -- Story 48.4 / migration 146: the ONE activation authority. This used to read
    -- the legacy preferences flag, which NO Epic-41 model reads any more.
    FROM {{ source('mirror', 'project_tax_fee_activation') }}
    WHERE COALESCE(CAST(tax_fees_active AS BOOLEAN), FALSE)
),

alloc AS (
    SELECT project_id, date, connector, breakdown_dimension, breakdown_value,
           measured_impressions
    FROM {{ ref('fee_tax_verification_allocation') }}
    WHERE row_kind = 'allocation'
),

fact_series AS (
    SELECT
        f.project_id                    AS project_id,
        CAST(f.date AS DATE)            AS date,
        f.connector                     AS connector,
        f.breakdown_dimension           AS breakdown_dimension,
        SUM(f.value)                    AS fact_value
    FROM {{ ref('fact_daily_kpi') }} f
    JOIN module_on m ON m.project_id = f.project_id
    WHERE f.metric = 'measured_impressions'
    GROUP BY f.project_id, CAST(f.date AS DATE), f.connector, f.breakdown_dimension
),

cardinality_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: the overlay has no allocation rows, so the anti-double-count'
        || ' assertions are vacuous. Run dbt/seeds/feetax/seed_fee_tax_verification.py'
        || ' and rebuild fact_daily_kpi BEFORE the overlay.' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM alloc) = 0
),

multi_series_break AS (
    SELECT
        a.project_id || '|' || CAST(a.date AS STRING) || '|' || a.connector AS subject,
        'DOUBLE_COUNT_FAIL: ' || CAST(COUNT(DISTINCT a.breakdown_dimension) AS STRING)
        || ' distinct breakdown_dimensions entered the allocation for this'
        || ' (project, date, connector). Exactly ONE canonical series may enter, or the'
        || ' verification fee is charged once per parallel series.' AS failure_reason
    FROM alloc a
    GROUP BY a.project_id, a.date, a.connector
    HAVING COUNT(DISTINCT a.breakdown_dimension) > 1
),

wrong_series_break AS (
    -- Where the connector emits campaign_id alongside another series, campaign_id must be
    -- the one chosen -- MIN() alone would pick alphabetically and silently change grain.
    SELECT
        a.project_id || '|' || CAST(a.date AS STRING) || '|' || a.connector AS subject,
        'CANONICAL_SERIES_FAIL: this connector emits a campaign_id series for'
        || ' measured_impressions, but the allocation ran at grain '
        || MIN(a.breakdown_dimension) || ' instead' AS failure_reason
    FROM alloc a
    JOIN fact_series fs
        ON  fs.project_id          = a.project_id
        AND fs.date                = a.date
        AND fs.connector           = a.connector
        AND fs.breakdown_dimension = 'campaign_id'
    GROUP BY a.project_id, a.date, a.connector
    HAVING MIN(a.breakdown_dimension) <> 'campaign_id'
        OR MAX(a.breakdown_dimension) <> 'campaign_id'
),

base_total_break AS (
    -- No under- or over-counting: the base summed by the model must be exactly the fact
    -- total of the series it selected.
    SELECT
        g.project_id || '|' || CAST(g.date AS STRING) || '|' || g.connector
            || '|' || g.breakdown_dimension AS subject,
        'BASE_TOTAL_FAIL: the overlay summed ' || CAST(g.model_impressions AS STRING)
        || ' measured impressions for this canonical series but fact_daily_kpi holds '
        || CAST(g.fact_value AS STRING) AS failure_reason
    FROM (
        SELECT
            a.project_id, a.date, a.connector, a.breakdown_dimension,
            SUM(a.measured_impressions) AS model_impressions,
            MAX(fs.fact_value)          AS fact_value
        FROM alloc a
        JOIN fact_series fs
            ON  fs.project_id          = a.project_id
            AND fs.date                = a.date
            AND fs.connector           = a.connector
            AND fs.breakdown_dimension = a.breakdown_dimension
        GROUP BY a.project_id, a.date, a.connector, a.breakdown_dimension
    ) g
    WHERE CAST(ROUND(g.fact_value) AS BIGINT) <> g.model_impressions
)

SELECT subject, failure_reason FROM cardinality_guard
UNION ALL SELECT subject, failure_reason FROM multi_series_break
UNION ALL SELECT subject, failure_reason FROM wrong_series_break
UNION ALL SELECT subject, failure_reason FROM base_total_break
{%- endif -%}
