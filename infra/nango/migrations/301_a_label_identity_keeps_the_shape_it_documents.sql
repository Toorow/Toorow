-- 301 — `app.dimension_labels.id` reprend la forme que sa propre colonne documente.
--
-- LE DEFAUT. La 106 déclare la colonne ainsi :
--
--     id TEXT PRIMARY KEY,   -- ULID prefixe : 'dlb_'
--
-- et n'a jamais posé de CHECK. La 105 a semé trois lignes PLATFORM sous des clés
-- déterministes — `dlb_27_8_audience_language` et ses deux sœurs — et la 174,
-- qui réparait le no-op de la 105, a répété exactement les mêmes. Mesuré en
-- préprod le 2026-08-23 : `SELECT id FROM app.dimension_labels` rend trois
-- lignes, les trois. Le contrat documenté n'a donc JAMAIS été vrai, et rien ne
-- l'aurait dit.
--
-- POURQUOI CE N'EST PAS COSMETIQUE. Un identifiant qui porte le nom de la story
-- qui l'a semé (`27_8`) est un identifiant qui parle d'un moment plutôt que d'une
-- ligne : il se lit comme une donnée, il invite à être construit ailleurs par
-- concaténation, et le jour où deux semis choisissent la même convention ils
-- entrent en collision sur une clé primaire. La forme opaque existe pour que
-- personne ne puisse la deviner ni la recomposer.
--
-- LES DEUX MIGRATIONS SONT APPLIQUEES, DONC IMMUABLES. On ne les ré-édite pas,
-- on corrige par celle-ci : c'est la règle que le dépôt tient depuis la 174.
--
-- AUCUNE REFERENCE A METTRE A JOUR, et ce n'est pas une supposition :
--   * aucune contrainte de clé étrangère ne cible `app.dimension_labels(id)` —
--     `grep -i "references.*dimension_labels" infra/nango/migrations/*.sql` est vide ;
--   * aucun code ne cite ces identifiants — `grep -rn "dlb_27_8"` sur le dépôt
--     entier ne rend que la review qui a nommé la dette et le journal des stories.
-- Les lecteurs de cette table joignent sur `canonical_dimension` et la portée
-- (c'est la cascade PROJECT > ORG > PLATFORM de `reduce_labels_by_specificity`),
-- jamais sur l'identifiant.
--
-- LES TROIS NOUVEAUX SONT DES LITTERAUX, frappés une fois à l'écriture de ce
-- fichier. Postgres n'a pas de générateur ULID, et une migration appliquée doit
-- rendre le même résultat partout où elle passe : une valeur tirée au vol
-- donnerait trois bases avec trois identités différentes pour la même ligne
-- semée. Ce que la contrainte gouverne est la FORME, pas l'instant du tirage.

BEGIN;

UPDATE app.dimension_labels
   SET id = 'dlb_01M0Q5PYQA76FV87Q87PKRFNQM', updated_at = now()
 WHERE id = 'dlb_27_8_audience_language';

UPDATE app.dimension_labels
   SET id = 'dlb_01M0Q5PYQBV24YZXF5T6JR3CC3', updated_at = now()
 WHERE id = 'dlb_27_8_content_language';

UPDATE app.dimension_labels
   SET id = 'dlb_01M0Q5PYQBV24YZXF5T6JR3CC4', updated_at = now()
 WHERE id = 'dlb_27_8_targeting_language';

-- LA CONTRAINTE, POSEE APRES LA REPRISE ET NON AVANT : posée d'abord, elle
-- referait échouer la migration sur les lignes qu'elle vient corriger.
--
-- L'alphabet est le Crockford base32 que les autres tables d'identité de ce
-- schéma exigent déjà (`ck_semantic_concepts_id`, `ck_semantic_views_id`) : `I`,
-- `L`, `O` et `U` en sont exclus, parce qu'ils se lisent comme 1, 1, 0 et V.
-- Une identité qu'on ne peut pas recopier au téléphone sans se tromper n'est pas
-- une identité.
--
-- `NOT VALID` n'est PAS utilisé : la table compte trois lignes et elles sont
-- toutes corrigées au-dessus, donc la validation est immédiate et une contrainte
-- non validée serait une contrainte qui ne dit pas encore la vérité.
ALTER TABLE app.dimension_labels
  DROP CONSTRAINT IF EXISTS ck_dimension_labels_id;

ALTER TABLE app.dimension_labels
  ADD CONSTRAINT ck_dimension_labels_id
  CHECK (id ~ '^dlb_[0-9A-HJKMNP-TV-Z]{26}$');

-- REFUSER PLUTOT QUE LAISSER PASSER. Si une ligne d'une autre base ne rentre pas
-- dans la forme, l'ADD CONSTRAINT ci-dessus échoue et la transaction entière est
-- annulée -- ce qui est le comportement voulu : une identité hors forme est un
-- fait à regarder, pas une ligne à effacer en silence.

COMMIT;
