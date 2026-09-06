-- test_fx_resolution_applied.sql -- Story 13.2 (AD-6), REECRIT le 2026-08-08.
--
-- CE QUE CE TEST DISAIT, ET POURQUOI IL NE POUVAIT PLUS LE DIRE.
--
-- Il affirmait `cost = cost_source_value * resolved_rate` : la conversion FX AU
-- STAGING. Ce n'est plus le lieu. La story 48.3 (apres 39.10) a deplace le locus
-- A LA LECTURE -- le staging PRESERVE le montant natif et RESOUT le taux, et c'est
-- `fact_daily_kpi` qui multiplie. `stg_meta_ads_daily.sql` le dit a sa ligne 78 :
-- `raw.spend AS cost`.
--
-- Consequence, et c'est la raison pour laquelle aucune fixture ne pouvait le rendre
-- vert : `cost` VAUT `cost_source_value`, donc `ABS(cost - cost_source_value * 0.92)`
-- fait 8 % de la valeur, tres au-dessus de la tolerance de 0,01 %. Le test echouait
-- quelle que soit la devise semee, et trois lectures successives de l'action item
-- AI-163 ont cherche du cote des devises avant de lire cette ligne-ci.
--
-- Son voisin `test_meta_cost_normalization.sql` a ete reecrit le 2026-08-04 pour
-- EXACTEMENT ce deplacement, et son en-tete porte l'avertissement qui vaut aussi
-- ici : << une assertion qui decrit un monde revolu est pire qu'aucune : elle est
-- crue, et elle pousserait a reparer le staging en y remettant la multiplication
-- que 48.3 en a retiree >>. Les deux tests etaient frappes par le meme changement ;
-- l'un a ete reecrit, l'autre epingle au cliquet des tests connus-rouges, ou il est
-- reste douze jours.
--
-- CE QU'IL VERIFIE MAINTENANT -- le contrat courant, en deux moities, sur le meme
-- perimetre qu'avant (les lignes dont une resolution change la devise source) :
--
--   1. LA RESOLUTION EST LUE. `fx_rate` doit etre le taux publie pour
--      (devise RESOLUE -> devise canonique du projet), et non celui de la devise
--      brute. C'est la seule chose qui distingue un staging qui applique AD-6 d'un
--      staging qui l'ignore, maintenant que plus personne ne multiplie ici.
--   2. LE NATIF EST PRESERVE. `cost` vaut exactement `cost_source_value` -- la
--      regression que 48.3 interdit, la meme moitie que le voisin garde de son cote.
--
-- POURQUOI DEUX PROJETS, ET PAS UNE DEVISE MIEUX CHOISIE. Ce test veut le taux de la
-- devise RESOLUE ; le voisin veut celui de la devise BRUTE, sur les lignes du meme
-- modele. Aucune devise unique ne satisfait les deux -- verifie le 2026-08-08 sur les
-- trois combinaisons possibles. La fixture porte donc deux populations disjointes :
-- `default` en USD sans resolution pour le voisin, `fx_conflict_dev` en EUR avec une
-- resolution vers USD pour celui-ci, invisible du voisin parce que sa devise brute
-- egale sa devise canonique.
--
-- GARDE D'ANTI-VACUITE, en deux temps parce qu'un seul ne suffit pas :
--   * aucune ligne surchargee => le test ne prouve rien, il echoue plutot que de
--     passer a vide (c'est cette garde-la qui tirait jusqu'ici, et elle avait
--     raison) ;
--   * le taux de la devise resolue doit DIFFERER de celui de la devise brute, sinon
--     lire la resolution ou l'ignorer rend le meme nombre et l'assertion 1 ne
--     distingue plus rien.
--
-- Un test dbt singulier ECHOUE s'il retourne des lignes (zero ligne = SUCCES).

--
-- UN VERDICT NE SE DEPOSE PAS SUR UN COMPTE QUI N A PAS PU ETRE PRIS (AI-314,
-- 2026-08-24). Ce test lit le miroir -- directement, ou a travers `dim_project`
-- qui n en vient que de la. `mirror_sync` differe ses ecritures BigQuery
-- (Phase B), donc en production le jeu de donnees `mirror` N EXISTE PAS : le
-- test y ECHOUAIT sur sa garde de cardinalite (aucune ligne surchargee) ou
-- ERRAIT sur la relation absente, et un test rouge est un code de sortie, donc
-- un projet sans marts. Ni l un ni l autre n est un defaut de staging : c est
-- une PREMISSE ABSENTE.
--
-- Il DECLINE donc de juger, en le DISANT (`TOOROW_SOURCE_ABSENT`, que le
-- nocturne lit et qui empeche le projet de rendre << ok >>). Ce n est pas un
-- assouplissement : la ou le miroir EST -- la boucle locale, la CI -- rien ne
-- bouge, les gardes d anti-vacuite restent armees et le test reste aussi
-- contraignant qu avant.
{%- set mirror_missing = toorow_absent_sources('mirror', ['project_preferences', 'fx_source_currency_bindings']) -%}
{%- if mirror_missing | length > 0 -%}
{{ toorow_absent_source_stub('mirror', mirror_missing, [
    ['cost_source_value', 'float'],
    ['cost', 'float'],
    ['resolved_source_currency', 'string'],
    ['fx_rate', 'float'],
    ['expected_rate', 'float'],
    ['failure_reason', 'string'],
]) }}
{%- else %}

WITH staged AS (
    SELECT
        s.project_id,
        s.cost_source_value,
        s.cost,
        s.cost_source_currency,
        s.fx_rate,
        COALESCE(dp.canonical_currency, 'EUR') AS to_currency
    FROM {{ ref('stg_meta_ads_daily') }} s
    LEFT JOIN {{ ref('dim_project') }} dp ON dp.project_id = s.project_id
    WHERE s.cost_source_value IS NOT NULL
),

resolutions AS (
    SELECT
        r.project_id,
        r.resolved_source_currency
    FROM {{ source('mirror', 'fx_source_currency_bindings') }} r
    WHERE r.target_field  = 'cost'
      AND r.source_module = 'meta-ads'
),

-- Le taux publie, borne par la meme fenetre de validite que la jointure du staging
-- (`CAST(raw.date AS DATE) BETWEEN fx.valid_from AND fx.valid_to`), pour ne pas
-- comparer `fx_rate` a un taux que le staging n'aurait pas pu choisir.
rates AS (
    SELECT from_currency, to_currency, rate
    FROM {{ ref('fx_rates') }}
    WHERE CAST('2026-03-05' AS DATE) BETWEEN valid_from AND valid_to
),

-- Les lignes que la resolution SURCHARGE : une resolution existe pour leur projet et
-- elle nomme une devise differente de la brute.
overridden AS (
    SELECT
        s.cost_source_value,
        s.cost,
        s.cost_source_currency,
        s.to_currency,
        s.fx_rate,
        r.resolved_source_currency
    FROM staged s
    JOIN resolutions r ON r.project_id = s.project_id
    WHERE r.resolved_source_currency <> s.cost_source_currency
),

cardinality_guard AS (
    SELECT
        CAST(NULL AS {{ toorow_float_type() }})  AS cost_source_value,
        CAST(NULL AS {{ toorow_float_type() }})  AS cost,
        CAST(NULL AS {{ toorow_string_type() }}) AS resolved_source_currency,
        CAST(NULL AS {{ toorow_float_type() }})  AS fx_rate,
        CAST(NULL AS {{ toorow_float_type() }})  AS expected_rate,
        'CARDINALITY_FAIL: aucune ligne surchargee -- la resolution ne change la '
        || 'devise d aucune ligne, donc ce test ne prouve rien. Semer '
        || 'dbt/seeds/fx/seed_fx_source_currency_bindings.py, qui pose le projet '
        || 'fx_conflict_dev en EUR avec une resolution vers USD.' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM overridden) = 0
),

-- Anti-vacuite 2 : si les deux taux sont egaux, lire la resolution ou l'ignorer rend
-- le meme nombre et l'assertion ci-dessous ne distingue plus rien.
indistinguishable_guard AS (
    SELECT
        CAST(NULL AS {{ toorow_float_type() }})  AS cost_source_value,
        CAST(NULL AS {{ toorow_float_type() }})  AS cost,
        o.resolved_source_currency,
        CAST(NULL AS {{ toorow_float_type() }})  AS fx_rate,
        CAST(NULL AS {{ toorow_float_type() }})  AS expected_rate,
        'VACUOUS_FAIL: le taux de la devise resolue egale celui de la devise brute -- '
        || 'appliquer la resolution ou l ignorer rend le meme nombre' AS failure_reason
    FROM overridden o
    JOIN rates rr ON rr.from_currency = o.resolved_source_currency
                 AND rr.to_currency   = o.to_currency
    JOIN rates rw ON rw.from_currency = o.cost_source_currency
                 AND rw.to_currency   = o.to_currency
    WHERE CAST(rr.rate AS {{ toorow_decimal_type(38, 9) }}) = CAST(rw.rate AS {{ toorow_decimal_type(38, 9) }})
),

-- 1. La resolution est LUE : le taux retenu est celui de la devise resolue.
resolution_applied AS (
    SELECT
        o.cost_source_value,
        o.cost,
        o.resolved_source_currency,
        o.fx_rate,
        r.rate AS expected_rate,
        CASE
            WHEN o.fx_rate IS NULL
                THEN 'RATE_UNRESOLVED_FAIL: fx_rate NULL sur une ligne surchargee -- '
                     || 'la conversion a la lecture rendra NULL en silence'
            ELSE 'RESOLUTION_IGNORED_FAIL: fx_rate <> taux publie pour la devise '
                 || 'RESOLUE -- le staging a garde le taux de la devise brute'
        END AS failure_reason
    FROM overridden o
    LEFT JOIN rates r
        ON r.from_currency = o.resolved_source_currency
       AND r.to_currency   = o.to_currency
    WHERE o.fx_rate IS NULL
       OR (r.rate IS NOT NULL
           AND ABS(CAST(o.fx_rate AS {{ toorow_decimal_type(38, 9) }}) - CAST(r.rate AS {{ toorow_decimal_type(38, 9) }})) > 0)
),

-- 2. Le natif est PRESERVE : le staging ne convertit pas (48.3).
native_preserved AS (
    SELECT
        o.cost_source_value,
        o.cost,
        o.resolved_source_currency,
        o.fx_rate,
        CAST(NULL AS {{ toorow_float_type() }}) AS expected_rate,
        'STAGING_CONVERTED_FAIL: cost <> cost_source_value -- une multiplication FX '
        || 'est revenue au staging (le locus est la LECTURE depuis 48.3)' AS failure_reason
    FROM overridden o
    WHERE CAST(o.cost AS {{ toorow_decimal_type(38, 9) }}) <> CAST(o.cost_source_value AS {{ toorow_decimal_type(38, 9) }})
)

SELECT cost_source_value, cost, resolved_source_currency, fx_rate, expected_rate,
       failure_reason
FROM cardinality_guard
UNION ALL
SELECT cost_source_value, cost, resolved_source_currency, fx_rate, expected_rate,
       failure_reason
FROM indistinguishable_guard
UNION ALL
SELECT cost_source_value, cost, resolved_source_currency, fx_rate, expected_rate,
       failure_reason
FROM resolution_applied
UNION ALL
SELECT cost_source_value, cost, resolved_source_currency, fx_rate, expected_rate,
       failure_reason
FROM native_preserved
{%- endif -%}
