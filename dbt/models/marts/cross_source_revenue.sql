-- cross_source_revenue: single-source day totals for revenue (Rule P) -- Story 15.7 (Epic 15).
-- MIROIR EXACT de cross_source_conversions.sql (Story 3.7) applique a la metrique 'revenue'.
-- Priorite DECLARATIVE : dbt/seeds/metric_source_priority.csv (revenue -> shopify=1, stripe=2 ;
-- aucun nom de connecteur en dur ici).
--
-- POURQUOI (AD-4, CRITIQUE) : revenue est emis par DEUX connecteurs (shopify 15.4 ET stripe 15.7).
-- Un paiement Stripe qui regle une commande Shopify mesure la MEME vente -- sommer revenue a travers
-- les deux connecteurs DOUBLE-COMPTERAIT cette vente. Cette vue choisit UNE source gagnante par
-- (project_id, date) selon la priorite declaree, exactement comme la conversion 3.7 choisit GA4.
-- Les lignes par source restent intactes dans fact_daily_kpi (WHERE metric='revenue').
--
-- logique en deux temps (identique a 3.7, review-3-7 F-1/F-2) :
--   1. choisir le CONNECTEUR gagnant par (project_id, date) via la priorite du seed ;
--   2. total = SUM sur TOUTES les lignes de ce connecteur sur UNE breakdown_dimension canonique
--      (MIN(breakdown_dimension)). Pour shopify comme pour stripe la seule dim est 'day_total',
--      donc la somme est triviale ; le pattern MIN reste correct si un connecteur revenue futur
--      emettait des series paralleles (sommer a travers les dims double-compterait -- lecon 1.6).
-- Story : 15.7 -- revenue Stripe et revenue Shopify jamais sommes dans un total croise (AD-4).
--
-- HONNÊTETÉ AD-9 (consommateurs : lire avant de comparer des jours) :
--   La source gagnante peut CHANGER d'un jour a l'autre selon la disponibilite des donnees :
--   shopify (priorite 1) est retenu sur les jours ou des commandes Shopify existent, stripe
--   (priorite 2) est retenu sur les jours couverts uniquement par Stripe (SaaS, services).
--   De plus, les ASSIETTES different : shopify mesure des commandes CREEES ce jour-la ;
--   stripe mesure des paiements ENCAISSES (la date de settlement peut decaler d'un jour ou plus
--   selon le cycle bancaire). Les perimetres de clients et de produits peuvent egalement differer
--   (boutique e-commerce vs abonnements SaaS). Une serie day-over-day qui traverse un changement
--   de source gagnante MELANGE donc deux assiettes distinctes. Les consommateurs qui comparent
--   des jours consignees sur une longue periode DOIVENT citer revenue_source dans leur analyse
--   et ne pas traiter la serie comme homogene sans verification.

WITH rev AS (
    SELECT project_id, date, connector, breakdown_dimension, value, money_gap_code, pull_id
    FROM {{ ref('fact_daily_kpi') }}
    WHERE metric = 'revenue'
),

winners AS (
    -- un connecteur gagnant par (project_id, date), par priorite declaree.
    SELECT project_id, date, connector
    FROM (
        SELECT
            c.project_id,
            c.date,
            c.connector,
            ROW_NUMBER() OVER (
                PARTITION BY c.project_id, c.date
                ORDER BY COALESCE(p.priority, 99), c.connector
            ) AS rnk
        FROM (SELECT DISTINCT project_id, date, connector FROM rev) c
        LEFT JOIN {{ ref('metric_source_priority') }} p
            ON p.metric = 'revenue' AND p.connector = c.connector
    )
    WHERE rnk = 1
),

canonical_dim AS (
    -- une breakdown_dimension par (project_id, date, connector) : chaque serie de dimension
    -- totalise independamment le jour ; on en choisit une deterministiquement.
    -- Partitionne par date pour qu'une dimension apparaissant en cours d'historique ne fasse
    -- pas silencieusement disparaitre les dates anterieures (fix canonical_dim global-gaps).
    SELECT project_id, date, connector, MIN(breakdown_dimension) AS dim
    FROM rev
    GROUP BY project_id, date, connector
)

SELECT
    f.project_id,
    f.date,
    -- A DAY WHOSE REVENUE COULD NOT BE CONVERTED STATES NO TOTAL (Story 48.3).
    -- `SUM()` SKIPS NULLs, and that is the whole defect this expression removes:
    -- over a winning source whose rows are partly unconvertible, a bare
    -- `SUM(f.value)` returned the convertible PART under the name of the day's
    -- total -- a silently understated figure, indistinguishable from a complete
    -- one. `MIN(CASE ... 0 ELSE 1 END) = 0` says "at least one contributing row
    -- carries no converted amount", and the total is then withheld rather than
    -- partial.
    CASE
        WHEN MIN(CASE WHEN f.value IS NULL THEN 0 ELSE 1 END) = 0 THEN NULL
        ELSE SUM(f.value)
    END              AS revenue_total,
    -- ... AND IT SAYS WHY, which is the other half of the same contract. The code
    -- is CARRIED from the contributing row that could not convert, never minted
    -- here: `fact_daily_kpi` already names WHICH thing is missing
    -- (`native_currency_missing`, an FX governance refusal, `fx_rate_unavailable`)
    -- and a second vocabulary for the same gap is a second thing to keep true.
    -- `MIN` ignores NULLs, so this is non-NULL EXACTLY when the CASE above yields
    -- NULL -- the pair `cross_source_revenue_total_null_iff_money_gap` asserts.
    -- Deterministic on a day carrying two different codes, like the `MIN` the
    -- money_evidence macro uses for the native currency.
    MIN(CASE WHEN f.value IS NULL THEN f.money_gap_code END) AS money_gap_code,
    MAX(f.connector) AS revenue_source,
    MAX(f.pull_id)   AS pull_id
FROM rev f
JOIN winners w
    ON w.project_id = f.project_id AND w.date = f.date AND w.connector = f.connector
JOIN canonical_dim d
    ON d.project_id = f.project_id AND d.date = f.date
   AND d.connector = f.connector AND d.dim = f.breakdown_dimension
GROUP BY f.project_id, f.date
