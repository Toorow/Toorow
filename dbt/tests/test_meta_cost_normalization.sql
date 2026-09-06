-- test_meta_cost_normalization.sql — Story 4.2 (AC5) / G-05, RÉÉCRIT le 2026-08-04.
--
-- CE QUE CE TEST DISAIT, ET POURQUOI IL NE PEUT PLUS LE DIRE.
--
-- Il affirmait le contrat story-4.2 / AD-6 : « la normalisation FX se fait UNE
-- FOIS, AU STAGING », donc `cost = cost_source_value * fx_rate`. Ce n'est plus le
-- lieu de la conversion. Le locus a été déplacé À LA LECTURE (macro
-- `fx_convert_at_read`, story 39.10 puis 48.3) : le staging PRÉSERVE le montant
-- natif et RÉSOUT le taux, et c'est `fact_daily_kpi` qui multiplie.
--
-- Mesuré sur la fixture locale le 2026-08-04, avant réécriture : les 420 lignes
-- meta-ads portent `cost_source_currency='USD'`, `fx_rate=0.92`, et
-- `cost = cost_source_value` pour les 420. L'ancienne assertion échouait donc sur
-- la totalité — non pas parce que la normalisation était fausse, mais parce
-- qu'elle n'a plus lieu ici. Une assertion qui décrit un monde révolu est pire
-- qu'aucune : elle est crue, et elle pousserait à « réparer » le staging en y
-- remettant la multiplication que 48.3 en a retirée.
--
-- CE QU'IL VÉRIFIE MAINTENANT — le contrat courant, en deux moitiés, aussi
-- contraignant que l'ancien et sur le même périmètre (lignes dont la devise
-- source diffère de la devise canonique du projet) :
--
--   1. LE NATIF EST PRÉSERVÉ. `cost` ne doit PAS avoir été converti au staging :
--      il vaut exactement `cost_source_value`. Une divergence signifie qu'une
--      multiplication est revenue en amont — la régression que 48.3 interdit, et
--      la raison d'être de HG-2/HG-3.
--   2. LE TAUX EST RÉSOLU, ET C'EST LE BON. `fx_rate` doit être non NULL et égal
--      au taux publié pour (devise source -> devise canonique du projet). Sans
--      cette moitié, un staging qui oublierait la résolution passerait : la
--      conversion à la lecture rendrait alors NULL partout, silencieusement.
--
-- AMENDÉ story 61.4. La moitié 2 disait « toute ligne cross-devise doit porter un
-- taux », ce qui n'est vrai que d'une fixture où chaque devise présente a un taux
-- publié. Ce n'est PAS le contrat produit : 48.3 a fait de l'absence de taux un
-- état LÉGITIME ET NOMMÉ (`fx_gap_code = 'fx_rate_unavailable'`, valeur NULL,
-- ligne conservée), précisément pour qu'un montant inconvertible ne soit plus
-- additionné à la parité. La fixture 61.4 en contient un — une campagne facturée
-- en JPY, devise que `dbt/seeds/fx_rates.csv` ne publie pas — et l'assertion
-- d'origine l'aurait déclaré défaut de staging.
-- La distinction est celle-ci, et elle rend le test PLUS contraignant, pas moins :
--   2a. un taux EST publié pour la paire -> le staging doit l'avoir résolu, et
--       exactement lui (le défaut réel : une résolution oubliée) ;
--   2b. aucun taux n'est publié -> `fx_rate` doit être NULL, jamais un taux
--       fabriqué (le défaut symétrique : un taux inventé passe inaperçu).
--
-- G-05 : `to_currency` est la `canonical_currency` du projet, jamais EUR en dur.
-- Garde de cardinalité : zéro ligne cross-devise = le test ne prouve rien, donc
-- il échoue plutôt que de passer à vide (même défaut que la Data MEDIUM-1
-- corrigée dans test_fx_resolution_applied).
--
-- Un test dbt singulier ÉCHOUE s'il retourne des lignes (zéro ligne = SUCCÈS).

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
{%- set mirror_missing = toorow_absent_sources('mirror', ['project_preferences']) -%}
{%- if mirror_missing | length > 0 -%}
{{ toorow_absent_source_stub('mirror', mirror_missing, [
    ['cost_source_value', 'float'],
    ['cost', 'float'],
    ['cost_source_currency', 'string'],
    ['to_currency', 'string'],
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
    WHERE s.cost_source_currency != COALESCE(dp.canonical_currency, 'EUR')
      AND s.cost_source_value IS NOT NULL
),
rates AS (
    SELECT from_currency, to_currency, rate
    FROM {{ ref('fx_rates') }}
),

-- Garde de cardinalité : sans ligne cross-devise DONT LA PAIRE EST PUBLIÉE, le
-- contrôle 2a retourne zéro ligne et le test passerait en ne prouvant rien. La
-- garde compte cet ensemble-là, pas l'ensemble cross-devise entier : depuis 61.4
-- la fixture contient aussi des lignes dont la paire n'est PAS publiée, et les
-- compter aurait suffi à éteindre la garde sans que 2a soit exercé.
cardinality_guard AS (
    SELECT
        CAST(NULL AS {{ toorow_float_type() }})  AS cost_source_value,
        CAST(NULL AS {{ toorow_float_type() }})  AS cost,
        CAST(NULL AS {{ toorow_string_type() }}) AS cost_source_currency,
        CAST(NULL AS {{ toorow_string_type() }}) AS to_currency,
        CAST(NULL AS {{ toorow_float_type() }})  AS fx_rate,
        CAST(NULL AS {{ toorow_float_type() }})  AS expected_rate,
        'CARDINALITY_FAIL: aucune ligne meta-ads cross-devise a taux publie -- le test ne prouve rien'
            AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (
        SELECT COUNT(*) FROM staged s
        WHERE EXISTS (
            SELECT 1 FROM rates r
            WHERE r.from_currency = s.cost_source_currency
              AND r.to_currency   = s.to_currency
        )
    ) = 0
),

-- 1. Le staging ne convertit pas : cost == cost_source_value, exactement.
native_preserved AS (
    SELECT
        s.cost_source_value,
        s.cost,
        s.cost_source_currency,
        s.to_currency,
        s.fx_rate,
        CAST(NULL AS {{ toorow_float_type() }}) AS expected_rate,
        'STAGING_CONVERTED_FAIL: cost <> cost_source_value -- une multiplication FX est '
        || 'revenue au staging (le locus est la LECTURE depuis 48.3)' AS failure_reason
    FROM staged s
    WHERE CAST(s.cost AS {{ toorow_decimal_type(38, 9) }}) <> CAST(s.cost_source_value AS {{ toorow_decimal_type(38, 9) }})
),

-- 2a. UN TAUX EST PUBLIÉ pour la paire : le staging doit l'avoir résolu, et
--     exactement lui. C'est le défaut réel — une résolution oubliée rendrait NULL
--     à la lecture, en silence.
rate_resolved AS (
    SELECT
        s.cost_source_value,
        s.cost,
        s.cost_source_currency,
        s.to_currency,
        s.fx_rate,
        r.rate AS expected_rate,
        CASE
            WHEN s.fx_rate IS NULL
                THEN 'RATE_UNRESOLVED_FAIL: fx_rate NULL alors qu''un taux EST publie pour '
                     || 'cette paire -- la conversion a la lecture rendra NULL en silence'
            ELSE 'RATE_MISMATCH_FAIL: fx_rate du staging <> taux publie (source -> canonique)'
        END AS failure_reason
    FROM staged s
    JOIN rates r
        ON r.from_currency = s.cost_source_currency
       AND r.to_currency   = s.to_currency
    WHERE s.fx_rate IS NULL
       OR ABS(CAST(s.fx_rate AS {{ toorow_decimal_type(38, 9) }}) - CAST(r.rate AS {{ toorow_decimal_type(38, 9) }})) > 0
),

-- 2b. AUCUN taux n'est publié : `fx_rate` doit rester NULL. Le défaut symétrique —
--     un taux fabriqué là où rien n'en publie — passerait autrement inaperçu, et
--     c'est exactement le `COALESCE(fx_rate, 1.0)` que 48.3 a retiré.
rate_not_fabricated AS (
    SELECT
        s.cost_source_value,
        s.cost,
        s.cost_source_currency,
        s.to_currency,
        s.fx_rate,
        CAST(NULL AS {{ toorow_float_type() }}) AS expected_rate,
        'RATE_FABRICATED_FAIL: fx_rate non NULL alors qu''AUCUN taux n''est publie pour '
        || 'cette paire -- un montant inconvertible doit rester inconvertible' AS failure_reason
    FROM staged s
    WHERE s.fx_rate IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM rates r
          WHERE r.from_currency = s.cost_source_currency
            AND r.to_currency   = s.to_currency
      )
)

SELECT cost_source_value, cost, cost_source_currency, to_currency, fx_rate,
       expected_rate, failure_reason
FROM cardinality_guard
UNION ALL
SELECT cost_source_value, cost, cost_source_currency, to_currency, fx_rate,
       expected_rate, failure_reason
FROM native_preserved
UNION ALL
SELECT cost_source_value, cost, cost_source_currency, to_currency, fx_rate,
       expected_rate, failure_reason
FROM rate_resolved
UNION ALL
SELECT cost_source_value, cost, cost_source_currency, to_currency, fx_rate,
       expected_rate, failure_reason
FROM rate_not_fabricated
{%- endif -%}
