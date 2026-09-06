-- T10 (Story 41.3, §D.1 / decision C4 / C.8 decision 10) -- THE NAMED, DISCRIMINATING
-- TRIPLE-COUNT TEST.
--
-- meta-ads and tiktok-ads each emit THREE parallel `cost` series (campaign_id /
-- adset_id / ad_id, one per data_level) and EACH ONE TOTALS THE DAY INDEPENDENTLY.
-- Compose fees over all three and every invoice triples. This test fails LOUDLY the
-- moment the canonical-dimension collapse is removed, instead of silently inflating
-- every number in the platform.
--
-- Four assertions:
--   A. exactly ONE distinct breakdown_dimension per (project_id, date, connector);
--   B. the chosen dimension IS 'campaign_id' wherever the connector emits it (C4);
--   C. SUM(net_media_micros) equals the micros-normalised total of the canonical
--      series, exactly;
--   D. it is STRICTLY LESS than the all-dimensions total wherever a connector emits
--      more than one series -- the assertion that actually discriminates.
-- Plus the C4 residual-risk guard: wherever a connector emits campaign_id ALONGSIDE
-- another cost series, the two series' daily totals must be EQUAL. Preferring
-- campaign_id is only as safe as MIN(breakdown_dimension) because meta/tiktok build
-- that series from ONE report grain; if a future connector ever emitted a
-- top-N-BOUNDED campaign_id series, the pick would silently under-total and this is
-- where that shows up.
--
-- ANTI-VACUITY GUARD: at least one (project, date, connector) must emit more than one
-- cost series, or assertion D is untestable. seed_fee_tax_mirror.py lands
-- feetax_dev_plan with all three meta-ads grains for exactly this reason.
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

fact_cost AS (
    SELECT f.project_id, f.date, f.connector, f.breakdown_dimension, f.breakdown_value, f.value
    FROM {{ ref('fact_daily_kpi') }} f
    JOIN module_on m ON m.project_id = f.project_id
    WHERE f.metric = 'cost'
),

series_per_group AS (
    SELECT
        project_id, date, connector,
        COUNT(DISTINCT breakdown_dimension) AS n_series,
        MAX(CASE WHEN breakdown_dimension = 'campaign_id' THEN 1 ELSE 0 END) AS has_campaign
    FROM fact_cost
    GROUP BY project_id, date, connector
),

series_totals AS (
    SELECT
        project_id, date, connector, breakdown_dimension,
        SUM({{ fee_tax_to_micros('value') }}) AS series_micros
    FROM fact_cost
    GROUP BY project_id, date, connector, breakdown_dimension
),

all_dim_total AS (
    SELECT project_id, date, connector, SUM(series_micros) AS all_series_micros
    FROM series_totals
    GROUP BY project_id, date, connector
),

ladder_group AS (
    SELECT
        project_id, date, connector,
        COUNT(DISTINCT breakdown_dimension) AS n_dims,
        MIN(breakdown_dimension)            AS the_dim,
        SUM(net_media_micros)               AS ladder_micros
    FROM {{ ref('fee_tax_ladder_daily') }}
    GROUP BY project_id, date, connector
),

anti_vacuity_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'ANTI_VACUITY_FAIL: no module-ON (project, date, connector) emits more than one'
        || ' cost series, so the triple-count assertion is untestable. Run'
        || ' seed_fee_tax_mirror.py (feetax_dev_plan lands all three meta-ads grains).'
            AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM series_per_group WHERE n_series > 1) = 0
),

cardinality_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: fee_tax_ladder_daily produced no row at all' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM ladder_group) = 0
),

-- A. one dimension per group
multi_dim AS (
    SELECT
        g.project_id || '|' || CAST(g.date AS STRING) || '|' || g.connector AS subject,
        'DOUBLE_COUNT_FAIL: the ladder kept ' || CAST(g.n_dims AS STRING)
            || ' breakdown dimensions for one (project, date, connector) -- every fee is'
            || ' being multiplied by that number' AS failure_reason
    FROM ladder_group g
    WHERE g.n_dims <> 1
),

-- B. campaign_id is preferred when available
wrong_pick AS (
    SELECT
        g.project_id || '|' || CAST(g.date AS STRING) || '|' || g.connector AS subject,
        'CANONICAL_PICK_FAIL: the connector emits campaign_id but the ladder ran at '
            || g.the_dim || ' -- C4 requires campaign_id so plan-scoped rules and the'
            || ' plan-line rollup resolve' AS failure_reason
    FROM ladder_group g
    JOIN series_per_group s
        ON  s.project_id = g.project_id
        AND s.date       = g.date
        AND s.connector  = g.connector
    WHERE s.has_campaign = 1
      AND g.the_dim <> 'campaign_id'
),

-- C. the ladder base equals the canonical series total exactly
base_drift AS (
    SELECT
        g.project_id || '|' || CAST(g.date AS STRING) || '|' || g.connector AS subject,
        'BASE_DRIFT_FAIL: SUM(net_media_micros) <> the canonical series cost total in'
        || ' micros' AS failure_reason
    FROM ladder_group g
    JOIN series_totals t
        ON  t.project_id          = g.project_id
        AND t.date                = g.date
        AND t.connector           = g.connector
        AND t.breakdown_dimension = g.the_dim
    WHERE g.ladder_micros <> t.series_micros
),

-- D. strictly less than the all-dimensions total when several series exist
not_strictly_less AS (
    SELECT
        g.project_id || '|' || CAST(g.date AS STRING) || '|' || g.connector AS subject,
        'DOUBLE_COUNT_FAIL: the connector emits several cost series but the ladder base'
        || ' is NOT strictly less than their combined total -- the collapse is not in'
        || ' force' AS failure_reason
    FROM ladder_group g
    JOIN series_per_group s
        ON  s.project_id = g.project_id AND s.date = g.date AND s.connector = g.connector
    JOIN all_dim_total a
        ON  a.project_id = g.project_id AND a.date = g.date AND a.connector = g.connector
    WHERE s.n_series > 1
      AND g.ladder_micros >= a.all_series_micros
),

-- The C4 residual-risk guard: a bounded campaign_id series would under-total.
campaign_series_not_reconciling AS (
    SELECT
        t1.project_id || '|' || CAST(t1.date AS STRING) || '|' || t1.connector AS subject,
        'CANONICAL_PICK_RISK_FAIL: the campaign_id cost series does not equal the '
            || t2.breakdown_dimension || ' series for the same day -- a BOUNDED'
            || ' campaign_id partition would make the C4 pick under-total' AS failure_reason
    FROM series_totals t1
    JOIN series_totals t2
        ON  t2.project_id = t1.project_id
        AND t2.date       = t1.date
        AND t2.connector  = t1.connector
        AND t2.breakdown_dimension <> t1.breakdown_dimension
    WHERE t1.breakdown_dimension = 'campaign_id'
      AND t1.series_micros <> t2.series_micros
)

SELECT subject, failure_reason FROM anti_vacuity_guard
UNION ALL SELECT subject, failure_reason FROM cardinality_guard
UNION ALL SELECT subject, failure_reason FROM multi_dim
UNION ALL SELECT subject, failure_reason FROM wrong_pick
UNION ALL SELECT subject, failure_reason FROM base_drift
UNION ALL SELECT subject, failure_reason FROM not_strictly_less
UNION ALL SELECT subject, failure_reason FROM campaign_series_not_reconciling
{%- endif -%}
