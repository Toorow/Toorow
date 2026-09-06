-- Les cibles de lien gouvernees : la base rattrape ce que le code accepte deja.
--
-- MESURE 2026-08-03, sur la base jetable a 200 migrations :
--
--   business_taxonomy.py  BUSINESS_TARGET_TYPES = {topic, procedure, target_field,
--       schema_doc, report_view, datastream, semantic_view, semantic_concept}
--   app.mdm_business_links      CHECK -> ... datastream          (migration 136)
--   app.context_path_resolutions CHECK -> ... report_view        (migration 130)
--
-- L'API valide donc `semantic_view` et `semantic_concept`, puis l'INSERT casse
-- sur la contrainte. `create_link` n'attrape que `UniqueViolation` : le refus
-- remonte en 500 `db_error / Context Hub is temporarily unavailable`, qui envoie
-- chercher du cote de Postgres pour un desaccord entre deux listes ecrites a la
-- main a deux endroits. C'est la meme classe que le `TypeError` de serialisation
-- repare le meme jour : l'ecriture metier est correcte, c'est la couche d'a cote
-- qui la fait echouer avec un message qui ment.
--
-- `context_path_resolutions` est encore un cran derriere : elle ignore aussi
-- `datastream`, ouvert il y a soixante-cinq migrations. Resoudre le chemin
-- gouverne d'un Datastream -- exactement ce que la 136 rendait possible --
-- echouait a l'ecriture de la trace.
--
-- POURQUOI LES DEUX DANS LA MEME MIGRATION : c'est une seule liste, tenue a
-- trois endroits. Ne rattraper que celle qui saigne garantit que la suivante
-- sera decouverte au clic.

ALTER TABLE app.mdm_business_links
    DROP CONSTRAINT IF EXISTS mdm_business_links_target_type_check;
ALTER TABLE app.mdm_business_links
    DROP CONSTRAINT IF EXISTS ck_mdm_business_links_target_type;
ALTER TABLE app.mdm_business_links
    ADD CONSTRAINT ck_mdm_business_links_target_type
    CHECK (target_type IN (
        'topic', 'procedure', 'target_field', 'schema_doc', 'report_view',
        'datastream', 'semantic_view', 'semantic_concept'
    ));

ALTER TABLE app.context_path_resolutions
    DROP CONSTRAINT IF EXISTS context_path_resolutions_target_type_check;
ALTER TABLE app.context_path_resolutions
    DROP CONSTRAINT IF EXISTS ck_context_path_resolutions_target_type;
ALTER TABLE app.context_path_resolutions
    ADD CONSTRAINT ck_context_path_resolutions_target_type
    CHECK (target_type IN (
        'topic', 'procedure', 'target_field', 'schema_doc', 'report_view',
        'datastream', 'semantic_view', 'semantic_concept'
    ));
