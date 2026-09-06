-- 120_drop_seeded_demo_content.sql
--
-- Supprime le contenu de DEMONSTRATION seme par les migrations 095 et 096.
--
-- ── Ce qui s'est passe ───────────────────────────────────────────────────────
-- L'epic 42 a cable deux ecrans du produit (Context ▸ Knowledge/Procedures,
-- Test ▸ Golden questions/Regression runs) et, pour qu'ils ne paraissent pas
-- vides, a livre leurs donnees DANS la migration :
--
--   095 : 3 "knowledge entries" + 5 procedures metier, signees
--         "Winston (Architect)", "Mary (Analyst)", "Paige (Tech Writer)"
--         -- des personas d'agents presentes comme des auteurs humains ;
--   096 : 8 golden questions + 6 runs d'evaluation aux scores fabriques
--         ("48/50, 96%, 2 regressions").
--
-- Le message du commit d'origine (3698f21) donne comme preuve de bon
-- fonctionnement : "both screens render the real seeded rows (3 knowledge
-- entries, 5 procedures) with real authors". L'ecran affichait donc ce qu'on
-- venait d'inventer pour lui.
--
-- ── Pourquoi supprimer plutot que corriger le texte ─────────────────────────
--   * Un ecran doit dire la verite. Vide, il dit "rien n'est encore defini",
--     ce qui est l'information JUSTE pour un projet neuf. Peuple de faux, il
--     ment -- et un score de qualite invente se lit comme une mesure.
--   * La reconciliation par metrique n'est pas a ecrire a la main : l'epic 27
--     la DERIVE des plateformes. core/metric_semantics_bootstrap.py propose les
--     source_metric_mappings a partir des manifests des connecteurs installes,
--     et core/metric_reconciliation.py resout la methode par la cascade
--     PROJET > ORG > PLATEFORME. Deux sources qui emettent le meme 'clicks'
--     s'alignent par cette cascade -- automatiquement, en fonction des
--     plateformes reellement branchees.
--
-- ── Pourquoi une nouvelle migration et non une edition de 095/096 ───────────
-- Le runner refuse une migration DEJA APPLIQUEE dont le contenu a change
-- (scripts/apply_migrations.py, "checksum drift"), et manifest.json fige le
-- sha256 de chaque fichier. Les migrations restent donc intactes ; celle-ci
-- s'execute APRES elles. Consequence voulue : sur une base neuve 095/096
-- sement, puis 120 nettoie -- le resultat final est le meme partout.
--
-- ── Portee ──────────────────────────────────────────────────────────────────
-- Supprime EXACTEMENT les lignes semees, identifiees par leur cle naturelle et
-- (pour metric_procedures) par leur auteur-persona : une ligne que quelqu'un
-- aurait reellement creee sur le projet 'default' n'est pas touchee. Les tables
-- restent : elles sont lues par des surfaces reelles.
--
-- Idempotent : re-jouer ne supprime rien de plus.

BEGIN;

DO $migration$
DECLARE
    removed INTEGER;
BEGIN
    -- 095 -- knowledge_entries (migrees vers context_topics par la 108 ; on
    -- nettoie les DEUX emplacements, la 108 ayant pu ou non passer ici).
    IF to_regclass('app.knowledge_entries') IS NOT NULL THEN
        DELETE FROM app.knowledge_entries
        WHERE project_id = 'default'
          AND author IN ('Winston (Architect)', 'Mary (Analyst)', 'Paige (Tech Writer)')
          AND title IN (
              'ROAS calculation & deduplication policy',
              'Canonical revenue vs commerce gross sales',
              'Post-click attribution reconciliation procedure'
          );
        GET DIAGNOSTICS removed = ROW_COUNT;
        RAISE NOTICE '120: % knowledge_entries de demonstration supprimee(s)', removed;
    END IF;

    -- Les memes entrees, si la 108 les a deja transposees en context_topics.
    -- id deterministe : 'top_legacy_' || md5('knowledge_entries:' || <id>), donc
    -- on ne peut pas les viser par id -- le titre + le projet suffisent, et la
    -- 108 n'a rien migre d'autre sous ces titres.
    IF to_regclass('app.context_topics') IS NOT NULL THEN
        DELETE FROM app.context_topics
        WHERE project_id = 'default'
          AND id LIKE 'top_legacy_%'
          AND title IN (
              'ROAS calculation & deduplication policy',
              'Canonical revenue vs commerce gross sales',
              'Post-click attribution reconciliation procedure'
          );
        GET DIAGNOSTICS removed = ROW_COUNT;
        RAISE NOTICE '120: % context_topics issus du seed 095 supprime(s)', removed;
        -- Note : app.context_topics_versions est append-only (trigger de la 031)
        -- et la 099 l'exclut sciemment de l'echappatoire RGPD. Les lignes de
        -- version correspondantes RESTENT, par conception. Elles ne sont lues
        -- par aucune surface une fois le topic parent supprime.
    END IF;

    -- 095 -- metric_procedures. L'auteur-persona est le discriminant : il
    -- distingue le seed d'une procedure qu'un humain aurait reellement saisie.
    IF to_regclass('app.metric_procedures') IS NOT NULL THEN
        DELETE FROM app.metric_procedures
        WHERE project_id = 'default'
          AND owner IN ('Winston (Architect)', 'Mary (Analyst)', 'Paige (Tech Writer)')
          AND metric IN ('ROAS', 'Conversions', 'Impressions', 'Blended CPA', 'Media revenue');
        GET DIAGNOSTICS removed = ROW_COUNT;
        RAISE NOTICE '120: % metric_procedures de demonstration supprimee(s)', removed;
    END IF;

    -- 096 -- golden_questions (8 questions, cle naturelle = project_id+question).
    IF to_regclass('app.golden_questions') IS NOT NULL THEN
        DELETE FROM app.golden_questions
        WHERE project_id = 'default'
          AND question IN (
              'What was total paid-media spend last month across all channels?',
              'Which campaign had the highest ROAS in Q2?',
              'How did organic search clicks trend over the last 8 weeks?',
              'What share of conversions is attributed to Meta Ads post-click?',
              'Break down revenue by country for the current quarter.',
              'Is reported spend consistent between Google Ads and GA4?',
              'What is the blended CPA across paid channels this month?',
              'Which datastreams are missing data for yesterday?'
          );
        GET DIAGNOSTICS removed = ROW_COUNT;
        RAISE NOTICE '120: % golden_questions de demonstration supprimee(s)', removed;
    END IF;

    -- 096 -- eval_runs (6 runs, cle naturelle = project_id+run_at).
    IF to_regclass('app.eval_runs') IS NOT NULL THEN
        DELETE FROM app.eval_runs
        WHERE project_id = 'default'
          AND run_at IN (
              TIMESTAMPTZ '2026-07-24 09:12:00+00',
              TIMESTAMPTZ '2026-07-23 18:40:00+00',
              TIMESTAMPTZ '2026-07-23 08:55:00+00',
              TIMESTAMPTZ '2026-07-22 17:03:00+00',
              TIMESTAMPTZ '2026-07-22 09:20:00+00',
              TIMESTAMPTZ '2026-07-21 16:48:00+00'
          );
        GET DIAGNOSTICS removed = ROW_COUNT;
        RAISE NOTICE '120: % eval_runs de demonstration supprime(s)', removed;
    END IF;
END
$migration$;

COMMIT;
