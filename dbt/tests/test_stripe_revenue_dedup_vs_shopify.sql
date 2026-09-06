-- test_stripe_revenue_dedup_vs_shopify.sql -- Story 15.7 (dedup revenue Stripe x Shopify, AD-4).
--
-- CRITIQUE et NON-TAUTOLOGIQUE : prouve que revenue Stripe et revenue Shopify ne fusionnent dans
-- AUCUN total croise. Le seed Stripe correle DELIBEREMENT une MAJORITE de charges Stripe a des
-- commandes Shopify (meme jour, meme revenue, client_reference_id = order_id) : ce sont les MEMES
-- ventes vues deux fois. Sommer naivement revenue a travers les deux connecteurs les double-compterait.
--
-- Le test s'articule en trois guards (un test singulier dbt ECHOUE s'il retourne des lignes) :
--
--   Guard A (NON-VACUITE) : il DOIT exister au moins un (project, date) ou shopify ET stripe
--     emettent tous deux du revenue. Sans ce chevauchement le test serait tautologique (rien a
--     dedupliquer). Si aucun jour ne chevauche -> on retourne une ligne 'no_overlap_days' (FAIL) :
--     le seed correle est casse et la preuve de dedup n'a pas de matiere.
--
--   Guard B (DEDUP EFFECTIVE) : sur CHAQUE jour de chevauchement, cross_source_revenue.revenue_total
--     doit etre STRICTEMENT INFERIEUR a la somme naive (shopify_revenue + stripe_revenue). Si la vue
--     avait somme les deux sources (bug de double-compte), revenue_total EGALERAIT la somme naive et
--     ce guard retournerait le jour. cross_source_revenue choisit UNE source gagnante (shopify,
--     priorite 1), donc revenue_total == shopify_revenue < somme naive des que stripe > 0 ce jour.
--
--   Guard C (SOURCE GAGNANTE = SHOPIFY) : sur les jours de chevauchement, revenue_source doit valoir
--     'shopify' (priorite 1 declaree dans metric_source_priority.csv). Toute autre valeur = la regle
--     de priorite n'est pas respectee.
--
-- Zero lignes = SUCCES.

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
{%- set mirror_missing = toorow_absent_sources('mirror', ['project_preferences']) -%}
{%- if mirror_missing | length > 0 %}
{{ toorow_absent_source_stub('mirror', mirror_missing, [['declined', 'string']]) }}
{%- else %}
WITH per_source AS (
    SELECT project_id, date, connector, SUM(value) AS rev
    FROM {{ ref('fact_daily_kpi') }}
    WHERE metric = 'revenue' AND connector IN ('shopify', 'stripe')
    GROUP BY project_id, date, connector
),

by_day AS (
    SELECT
        project_id,
        date,
        MAX(CASE WHEN connector = 'shopify' THEN rev END) AS shopify_rev,
        MAX(CASE WHEN connector = 'stripe'  THEN rev END) AS stripe_rev
    FROM per_source
    GROUP BY project_id, date
),

overlap_days AS (
    SELECT project_id, date, shopify_rev, stripe_rev,
           shopify_rev + stripe_rev AS naive_sum
    FROM by_day
    WHERE shopify_rev IS NOT NULL AND stripe_rev IS NOT NULL
      AND shopify_rev > 0 AND stripe_rev > 0
),

csr AS (
    SELECT project_id, date, revenue_total, revenue_source
    FROM {{ ref('cross_source_revenue') }}
),

-- Guard A: non-vacuite -- au moins un jour de chevauchement doit exister.
guard_no_overlap AS (
    SELECT
        'no_overlap_days' AS check_type,
        NULL AS project_id, NULL AS date,
        NULL AS revenue_total, NULL AS naive_sum, NULL AS revenue_source
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE NOT EXISTS (SELECT 1 FROM overlap_days)
),

-- Guard B: dedup effective -- cross_source_revenue < somme naive sur chaque jour de chevauchement.
guard_not_summed AS (
    SELECT
        'revenue_summed_across_sources' AS check_type,
        o.project_id, o.date,
        c.revenue_total, o.naive_sum, c.revenue_source
    FROM overlap_days o
    JOIN csr c ON c.project_id = o.project_id AND c.date = o.date
    -- si la vue avait somme les deux sources, revenue_total >= naive_sum (a la tolerance pres).
    --
    -- A WITHHELD TOTAL IS NOT A DEDUP FAILURE, and the skip is written rather than
    -- inherited from NULL comparison semantics. Since `cross_source_revenue` states
    -- NO total on a day whose winning source could not be converted (Story 48.3,
    -- criterion [25] of execution-substrate), such a day carries no figure to
    -- compare with the naive sum -- and the reason it carries none is stated in its
    -- own `money_gap_code`, which is where a reader learns it. Coercing that NULL
    -- to 0 here would turn "we do not know" into a silent proof of dedup.
    WHERE c.revenue_total IS NOT NULL
      AND c.revenue_total >= o.naive_sum - 0.001
),

-- Guard C: la source gagnante est shopify (priorite 1) sur les jours de chevauchement.
guard_wrong_winner AS (
    SELECT
        'winner_not_shopify' AS check_type,
        o.project_id, o.date,
        c.revenue_total, o.naive_sum, c.revenue_source
    FROM overlap_days o
    JOIN csr c ON c.project_id = o.project_id AND c.date = o.date
    WHERE c.revenue_source <> 'shopify'
)

SELECT check_type, project_id, date, revenue_total, naive_sum, revenue_source FROM guard_no_overlap
UNION ALL
SELECT check_type, project_id, date, revenue_total, naive_sum, revenue_source FROM guard_not_summed
UNION ALL
SELECT check_type, project_id, date, revenue_total, naive_sum, revenue_source FROM guard_wrong_winner
{%- endif -%}
