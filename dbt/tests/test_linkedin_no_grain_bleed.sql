-- test_linkedin_no_grain_bleed.sql -- Story 15.3 (anti-regression double-compte grain).
--
-- Preuve directe que chaque serie breakdown du mart LinkedIn est construite UNIQUEMENT
-- depuis les lignes de son propre pivot (data_level). Pattern identique a TikTok
-- test_tiktok_no_grain_bleed.sql (lecon review-15-2 F-1).
--
-- Pour chaque (project_id, date, metric), la valeur mart d'une serie de breakdown DOIT
-- egaliser le total staging du meme data_level uniquement :
--   campaign_id      series  == SUM(stg_campaign_daily WHERE campaign_id IS NOT NULL)
--   campaign_group_id series == SUM(stg_campaign_group_daily WHERE campaign_group_id IS NOT NULL)
--
-- Sans le filtre data_level (ou si les deux series etaient fusionnees), la serie
-- campaign_group_id sommer les campagnes ET leur groupe -> double-compte.
-- Avec le filtre data_level en staging, les deux series sont mutuellement exclusives
-- et coherentes avec leurs sources respectives.
--
-- Un test singulier dbt ECHOUE quand il retourne des lignes (zero lignes = succes).
-- Tolerance relative 0.001 pour les arrondis virgule flottante.

{% set campaign_metrics = ["cost", "impressions", "clicks", "conversions"] %}

WITH mart_campaign AS (
    SELECT project_id, date, metric, SUM(value) AS mart_total
    FROM {{ ref('fact_daily_kpi') }}
    WHERE connector = 'linkedin-ads'
      AND breakdown_dimension = 'campaign_id'
    GROUP BY project_id, date, metric
),

mart_group AS (
    SELECT project_id, date, metric, SUM(value) AS mart_total
    FROM {{ ref('fact_daily_kpi') }}
    WHERE connector = 'linkedin-ads'
      AND breakdown_dimension = 'campaign_group_id'
    GROUP BY project_id, date, metric
),

stg_campaign AS (
    {% for col in campaign_metrics %}
    SELECT
        project_id,
        date,
        '{{ col }}'  AS metric,
        SUM(CAST({{ col }} AS DOUBLE)) AS stg_total
    FROM {{ ref('stg_linkedin_ads_campaign_daily') }}
    WHERE campaign_id IS NOT NULL
    GROUP BY project_id, date
    {% if not loop.last %}UNION ALL{% endif %}
    {% endfor %}
),

stg_group AS (
    {% for col in campaign_metrics %}
    SELECT
        project_id,
        date,
        '{{ col }}'  AS metric,
        SUM(CAST({{ col }} AS DOUBLE)) AS stg_total
    FROM {{ ref('stg_linkedin_ads_campaign_group_daily') }}
    WHERE campaign_group_id IS NOT NULL
    GROUP BY project_id, date
    {% if not loop.last %}UNION ALL{% endif %}
    {% endfor %}
)

-- Serie campaign : mart vs staging campaign grain
SELECT
    'campaign' AS series,
    m.project_id, m.date, m.metric,
    m.mart_total, s.stg_total
FROM mart_campaign m
JOIN stg_campaign s
    ON s.project_id = m.project_id AND s.date = m.date AND s.metric = m.metric
WHERE ABS(m.mart_total - s.stg_total) > 0.001 * NULLIF(ABS(s.stg_total), 0)

UNION ALL

-- Serie campaign_group : mart vs staging campaign_group grain
SELECT
    'campaign_group' AS series,
    m.project_id, m.date, m.metric,
    m.mart_total, s.stg_total
FROM mart_group m
JOIN stg_group s
    ON s.project_id = m.project_id AND s.date = m.date AND s.metric = m.metric
WHERE ABS(m.mart_total - s.stg_total) > 0.001 * NULLIF(ABS(s.stg_total), 0)
