-- 121_purge_test_fixture_rows.sql
--
-- Supprime les lignes de FIXTURES laissees en base par les suites pg-gated.
--
-- ── Ce qui s'est passe ───────────────────────────────────────────────────────
-- `TEST_POSTGRES_DSN` a pointe sur la base de PRODUCTION. Or ces suites ne font
-- pas que lire : elles INSERENT et COMMITENT (il faut un vrai commit pour
-- prouver une contrainte). Chaque execution y a donc depose son jeu de lignes,
-- dont une partie au scope PLATEFORME (project_id NULL) -- c'est-a-dire
-- visibles depuis TOUS les projets de TOUS les clients (AD-5).
--
-- Effet observe le 2026-07-27 : le mindmap d'un projet cree le jour meme
-- affichait 15 noeuds -- 14 procedures `roas_analysis_<hex>` et un topic
-- "ROAS" -- qu'aucun utilisateur n'avait ecrits. 14 executions de
-- server/tests/integration/test_context_search_seams.py entre 21h32 et 22h05
-- le 25/07.
--
-- La cause est traitee en amont (server/tests/conftest.py refuse desormais une
-- DSN distante non declaree jetable, et un balayage de fin de session reprend
-- ce qui passe entre les mailles). Cette migration nettoie l'existant.
--
-- ── Deux familles, et rien d'autre ──────────────────────────────────────────
--   1. Lignes au scope PLATEFORME portant une signature de fixture
--      (`roas_analysis_*`, le topic "ROAS"/"platform roas def", created_by
--      'test'). Visees nommement : le scope plateforme contient aussi des
--      donnees legitimes, on ne balaie pas au motif du scope.
--   2. Lignes rattachees a un projet QUI N'EXISTE PAS (`projA_live`,
--      `projB_live`, `proj_conc`, `proj_arc`, `proj_test`, `proj_<hex>`,
--      `default`...). La regle est structurelle -- NOT EXISTS sur app.projects,
--      pas une liste de noms -- donc elle ne peut pas toucher les donnees d'un
--      projet reel, aujourd'hui ni demain.
--
-- ── Pourquoi chaque suppression est isolee ──────────────────────────────────
-- Premiere tentative : un seul bloc, toutes les suppressions a la suite. Il a
-- echoue en bloc sur
--     "file_source_templates is append-only: DELETE ... is forbidden
--      (immutable template version)"
-- et a donc TOUT annule, y compris les suppressions deja passees. Une table
-- protegee ne doit pas emporter le nettoyage des autres : chaque regle
-- s'execute donc dans son propre sous-bloc, et un refus est SIGNALE
-- (RAISE NOTICE) au lieu d'etre contourne. La liste des tables protegees n'est
-- pas devinee ici : elle se decouvre a l'execution, et se lit dans les notices.
--
-- ── Ce que cette migration NE fait PAS ──────────────────────────────────────
--   * app.context_topics_versions / procedures_versions / schema_context_versions
--     sont append-only (trigger de la 031) et la 099 les exclut DELIBEREMENT de
--     l'echappatoire `app.rgpd_erasure` ("donnees de reference globales, aucune
--     erasure de tenant n'a a les supprimer"). Les ~48 lignes de version ecrites
--     par les tests restent donc en base : les enlever demanderait de defaire un
--     trigger, ce qui est une decision humaine, pas un effet de bord de purge.
--     Elles ne sont lues par aucune surface une fois leur parent supprime.
--   * L'organisation `qa-e2e-*` creee par le harnais e2e n'est PAS supprimee
--     ici. Une suppression SQL laisserait ses datasets BigQuery orphelins :
--     drop_org_schemas n'est appele que par DELETE /api/organizations. Passer
--     par `uv run python e2e/cleanup.py --apply`.
--
-- Idempotent : re-jouer ne supprime rien de plus. Sur une base neuve, aucune de
-- ces lignes n'existe et la migration est un no-op complet.

BEGIN;

DO $migration$
DECLARE
    -- (table, predicat). Le predicat porte sur l'alias `t`.
    rules CONSTANT text[][] := ARRAY[
        -- 1. Scope plateforme -- signatures de fixtures, visees nommement.
        ARRAY['procedures',
              't.project_id IS NULL AND t.name LIKE ''roas_analysis_%'''],
        ARRAY['context_topics',
              't.project_id IS NULL AND t.title = ''ROAS'''
              ' AND t.body_md = ''platform roas def'''],
        ARRAY['mdm_canonical_fields',
              't.project_id IS NULL AND t.created_by = ''test'''],
        -- 2. Projets inexistants -- regle structurelle, jamais une liste de noms.
        ARRAY['context_topics', 'ORPHAN_PROJECT'],
        ARRAY['procedures', 'ORPHAN_PROJECT'],
        ARRAY['mdm_canonical_fields', 'ORPHAN_PROJECT'],
        ARRAY['file_source_templates', 'ORPHAN_PROJECT'],
        ARRAY['datastream_outbox', 'ORPHAN_PROJECT'],
        -- Telemetrie d'adherence (core/adherence.py) accumulee sur le projet
        -- 'default' pendant les sessions de dev : elle ne mesure aucun usage reel.
        ARRAY['query_adherence', 'ORPHAN_PROJECT'],
        -- Traces d'audit visant une org ou un projet qui n'existent pas, et dont
        -- les entites elles-memes ont disparu : elles ne documentent plus rien.
        ARRAY['metric_semantics_audit',
              '(t.org_id IS NOT NULL AND NOT EXISTS'
              ' (SELECT 1 FROM app.organizations o WHERE o.id = t.org_id))'
              ' OR (t.project_id IS NOT NULL AND NOT EXISTS'
              ' (SELECT 1 FROM app.projects p WHERE p.id = t.project_id))']
    ];
    orphan CONSTANT text :=
        't.project_id IS NOT NULL AND NOT EXISTS'
        ' (SELECT 1 FROM app.projects p WHERE p.id = t.project_id)';
    tbl       text;
    predicate text;
    removed   INTEGER;
    total     INTEGER := 0;
    refused   INTEGER := 0;
    i         INTEGER;
BEGIN
    FOR i IN 1 .. array_length(rules, 1) LOOP
        tbl := rules[i][1];
        predicate := rules[i][2];
        IF predicate = 'ORPHAN_PROJECT' THEN
            predicate := orphan;
        END IF;

        IF to_regclass('app.' || quote_ident(tbl)) IS NULL THEN
            RAISE NOTICE '121: app.% absente -- regle ignoree', tbl;
            CONTINUE;
        END IF;

        BEGIN
            EXECUTE format('DELETE FROM app.%I t WHERE %s', tbl, predicate);
            GET DIAGNOSTICS removed = ROW_COUNT;
            total := total + removed;
            IF removed > 0 THEN
                RAISE NOTICE '121: % ligne(s) supprimee(s) dans app.%', removed, tbl;
            END IF;
        EXCEPTION WHEN OTHERS THEN
            -- Protection append-only (ou toute autre garde) : on le DIT, on ne
            -- la contourne pas. Un dechet signale vaut mieux qu'un dechet tu.
            refused := refused + 1;
            RAISE NOTICE '121: REFUS sur app.% -- % (ces lignes restent)', tbl, SQLERRM;
        END;
    END LOOP;

    RAISE NOTICE '121: % ligne(s) de fixture supprimee(s), % regle(s) refusee(s)',
        total, refused;
END
$migration$;

COMMIT;
