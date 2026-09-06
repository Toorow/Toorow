-- Separer les mots-cles des metriques gouvernees -- et debloquer la validation.
--
-- MESURE 2026-08-03. `mdm_tags` nomme des metriques GOUVERNEES : la console
-- l'intitule << MDM tags — Canonical metrics >> et le lie par cases a cocher a
-- `app.target_fields`. Mais aucun champ LIBRE n'existait, alors tout le monde
-- s'en servait comme d'une folksonomie -- y compris la procedure que le produit
-- seme lui-meme, posee par la migration 196 :
--
--     mdm_tags:
--       - platform-operating-procedure
--       - datastream
--       - context
--       - testing
--       - measurement
--
-- Sur les treize champs approuves du catalogue, AUCUN de ces cinq ne resout.
--
-- CONSEQUENCE : `mdm_tags` ne pouvait pas etre valide contre le catalogue. Le
-- rendre strict aurait rendu la procedure du produit non modifiable -- et une
-- validation qui casse la donnee du produit n'est pas une rigueur, c'est une
-- panne. C'est ce qui bloquait AI-156.
--
-- CE QUE FAIT CETTE MIGRATION : elle deplace ces cinq valeurs vers `keywords`,
-- le champ libre valide depuis aujourd'hui. Rien n'est perdu -- depuis que
-- `context_search` compare le frontmatter, un mot-cle sert vraiment le rappel,
-- et il le sert autant sous son vrai nom.
--
-- ⚠️ ELLE NE REECRIT PAS LA MIGRATION 196. Une migration appliquee ne se
-- re-edite jamais ; on corrige par la suivante. Et elle ne touche QUE la ligne
-- que le produit a semee : une procedure ecrite par quelqu'un d'autre lui
-- appartient, meme si ses tags ne resolvent pas.

DO $$
DECLARE
    v_front TEXT;
BEGIN
    IF to_regclass('app.procedures') IS NULL THEN
        RAISE NOTICE '203: skip -- app.procedures does not exist';
        RETURN;
    END IF;

    SELECT frontmatter_yaml INTO v_front
      FROM app.procedures
     WHERE project_id IS NULL
       AND name = 'platform-operating-procedure'
       AND status <> 'archived';

    IF v_front IS NULL THEN
        RAISE NOTICE '203: skip -- the seeded platform procedure is absent';
        RETURN;
    END IF;

    IF v_front NOT LIKE '%mdm_tags:%' THEN
        RAISE NOTICE '203: skip -- already carries no mdm_tags';
        RETURN;
    END IF;

    -- Le bloc est connu mot pour mot (migration 196) : on renomme la cle plutot
    -- que de reconstruire un YAML, ce qui laisserait tomber tout ce qu'on n'a
    -- pas pense a recopier.
    v_front := replace(v_front, E'mdm_tags:\n', E'keywords:\n');

    UPDATE app.procedures
       SET frontmatter_yaml = v_front, updated_at = now()
     WHERE project_id IS NULL
       AND name = 'platform-operating-procedure'
       AND status <> 'archived';

    -- ⚠️ ON AJOUTE UNE VERSION, ON N'EN REECRIT AUCUNE.
    -- `app.procedures_versions` est append-only : le declencheur
    -- `reject_context_version_mutation` refuse tout UPDATE, et il a raison --
    -- un historique qu'on peut reecrire ne prouve plus rien. Premiere version de
    -- cette migration : un UPDATE, refuse par la base. La bonne forme est celle
    -- que le contrat impose, et elle est meilleure : le renommage devient une
    -- version datee et attribuee, donc visible dans l'historique de la Skill.
    INSERT INTO app.procedures_versions
        (procedure_id, project_id, name, description, frontmatter_yaml, body_md,
         status, owner, created_by, created_at, updated_at, version_number,
         changed_by, changed_at)
    SELECT p.id, p.project_id, p.name, p.description, v_front, p.body_md,
           p.status, p.owner, p.created_by, p.created_at, now(),
           (SELECT max(version_number) + 1 FROM app.procedures_versions
             WHERE procedure_id = p.id),
           'system:migration-203', now()
      FROM app.procedures p
     WHERE p.project_id IS NULL
       AND p.name = 'platform-operating-procedure'
       AND p.status <> 'archived';

    RAISE NOTICE '203: keywords separated from mdm_tags on the seeded procedure';
END $$;
