-- Le type de cible `canonical_field` : la base rattrape ce que le code accepte deja.
--
-- MESURE 2026-08-18, SUR LE DEPLOIEMENT (mcp-server-00182-5j2, HEAD aacb2dd1) :
--
--   POST /api/context/business-links {target_type: "canonical_field", ...}
--     -> 500 `db_error / Context Hub is temporarily unavailable`
--   ck_mdm_business_links_target_type (prod) -> 8 types, sans `canonical_field`
--
-- C'est, mot pour mot, la classe que la migration 201 a documentee et reparee
-- pour `semantic_view` / `semantic_concept` : l'API valide le type
-- (`business_taxonomy.BUSINESS_TARGET_TYPES`, etendu par aacb2dd1 / AI-298),
-- puis l'INSERT casse sur la contrainte CHECK, et le refus remonte en 500 avec
-- un message qui envoie chercher une panne Postgres. La 201 avertissait :
-- << Ne rattraper que celle qui saigne garantit que la suivante sera decouverte
-- au clic. >> La suivante est celle-ci, decouverte au clic du parcours QA
-- (projet qa-e2e-probe-ds, champ mdm_01M0904XBJKWMK4GZHG1DCXKA3).
--
-- Les DEUX contraintes dans la meme migration, pour la raison de la 201 :
-- c'est une seule liste tenue a trois endroits, et `context_path_resolutions`
-- ecrit la trace d'un lien resolu -- un lien qui s'insere mais dont le chemin
-- gouverne ne peut pas s'ecrire serait la meme panne un cran plus tard.

ALTER TABLE app.mdm_business_links
    DROP CONSTRAINT IF EXISTS ck_mdm_business_links_target_type;
ALTER TABLE app.mdm_business_links
    ADD CONSTRAINT ck_mdm_business_links_target_type
    CHECK (target_type IN (
        'topic', 'procedure', 'target_field', 'canonical_field', 'schema_doc',
        'report_view', 'datastream', 'semantic_view', 'semantic_concept'
    ));

ALTER TABLE app.context_path_resolutions
    DROP CONSTRAINT IF EXISTS ck_context_path_resolutions_target_type;
ALTER TABLE app.context_path_resolutions
    ADD CONSTRAINT ck_context_path_resolutions_target_type
    CHECK (target_type IN (
        'topic', 'procedure', 'target_field', 'canonical_field', 'schema_doc',
        'report_view', 'datastream', 'semantic_view', 'semantic_concept'
    ));
