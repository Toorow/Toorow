-- 174_the_language_labels_the_105_never_wrote.sql
--
-- Story 27.8 -- REPARATION MESUREE. Les trois libelles PLATFORM de la famille
-- « langue » n'ont jamais ete ecrits. Pas une regression : ils ne l'ont JAMAIS ete.
--
-- CE QUI S'EST PASSE, EXACTEMENT.
-- La migration 105 se termine par un bloc DO qui insere les trois libelles sous
-- la garde `IF to_regclass('app.dimension_labels') IS NOT NULL THEN` -- son
-- commentaire annonce « les trois dimensions (103) », donc son auteur croyait la
-- table creee par la 103. Elle ne l'est pas : `103_fix_external_dispatch_null_source_kind`
-- ne cree rien de tel, et `app.dimension_labels` nait dans la **106**, APRES.
-- Au moment ou la 105 s'execute la table n'existe pas encore, la garde est FAUSSE,
-- le bloc ne fait rien, et -- c'est la ou ca fait mal -- il ne fait rien EN SILENCE :
-- une garde d'existence ne distingue pas « la table est absente parce qu'on rejoue
-- une base partielle » de « la table est absente parce que je passe trop tot ».
--
-- Mesure du 2026-08-01 sur la base reelle, avant cette migration :
--   toorow_meta.schema_migrations -> 105 et 106 'applied' le 2026-07-27 08:14:45
--   SELECT count(*) FROM app.dimension_labels;  -> 0
--
-- POURQUOI UNE MIGRATION DE PLUS, ET PAS UNE CORRECTION DE LA 105.
-- La 105 est appliquee, donc immuable : son sha256 est au manifeste et une
-- re-edition ferait diverger le checksum de toute base deja a jour. La regle du
-- depot est explicite -- on corrige par la SUIVANTE et on regenere le manifeste.
--
-- CE QUE CETTE MIGRATION FAIT, ET RIEN D'AUTRE.
-- Elle rejoue le meme INSERT, sans la garde d'existence (la table existe depuis la
-- 106, qui la precede maintenant pour de bon) et avec les memes identifiants, le
-- meme ON CONFLICT et les memes libelles. Aucune table, aucune colonne, aucun
-- index, aucune contrainte : la forme de 105/106 est intacte.
--
-- LE LIBELLE APPARTIENT AU CLIENT (27.9). Ces trois lignes sont des DEFAUTS de
-- portee PLATFORM, pas une decision : la cascade PROJECT > ORG > PLATFORM de la 106
-- fait qu'une ligne ORG ou PROJET les bat sans rien supprimer. L'identifiant stable
-- (`audience_language`, ...) ne bouge jamais et n'est pas ce que l'utilisateur lit ;
-- le libelle, si. Un projet qui appelle ca « Langue de l'audience » ecrit sa ligne
-- et celle-ci devient invisible pour lui.
--
-- IDEMPOTENCE. `ON CONFLICT ... DO NOTHING` vise exactement
-- `uq_dimension_labels_scope_key (scope_level, COALESCE(org_id,''),
-- COALESCE(project_id,''), canonical_dimension)` de la 106 -- le COALESCE est
-- obligatoire, NULL <> NULL laisserait passer deux lignes PLATFORM. Rejouer cette
-- migration sur une base qui la porte deja n'ecrit rien, et n'ECRASE surtout aucun
-- libelle qu'un humain aurait corrige entre-temps : DO NOTHING, jamais DO UPDATE.
--
-- RGPD : rien a ajouter. `app.dimension_labels` est mutable, sans garde
-- append-only, et ses FK ON DELETE CASCADE emportent deja les lignes ORG/PROJET a
-- l'effacement -- ces trois-ci sont PLATFORM et survivent, comme tout defaut.
--
-- Application :
--     psql -U connector -d connector -f /migrations/174_the_language_labels_the_105_never_wrote.sql
-- ---------------------------------------------------------------------------

BEGIN;

INSERT INTO app.dimension_labels
    (id, canonical_dimension, display_label, description, scope_level, org_id,
     project_id, created_by, created_at, updated_at)
VALUES
    ('dlb_27_8_audience_language', 'audience_language', 'Audience language',
     'Language observed on the person reached (browser / device / user setting).',
     'PLATFORM', NULL, NULL, 'system', now(), now()),
    ('dlb_27_8_content_language', 'content_language', 'Content language',
     'Language of the asset actually served (the creative / the content).',
     'PLATFORM', NULL, NULL, 'system', now(), now()),
    ('dlb_27_8_targeting_language', 'targeting_language', 'Targeting language',
     'Language declared as targeted (a delivery setting, not an observation).',
     'PLATFORM', NULL, NULL, 'system', now(), now())
ON CONFLICT (scope_level, COALESCE(org_id, ''), COALESCE(project_id, ''),
             canonical_dimension) DO NOTHING;

COMMIT;
