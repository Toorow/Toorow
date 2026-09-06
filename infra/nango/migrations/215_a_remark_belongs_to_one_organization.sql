-- 215_a_remark_belongs_to_one_organization.sql
--
-- UNE REMARQUE APPARTIENT A UNE ORGANISATION, ET SA CLE D'UNICITE L'IGNORAIT.
-- La migration 202 clefe `uq_context_review_request_open` sur
-- (node_type, node_id, node_version, requested_by, md5(note)) -- aucune de ces
-- cinq colonnes ne nomme le locataire. Tant que les remarques etaient posees a
-- la main par des humains, chacun avec son identifiant, la collision restait
-- theorique.
--
-- LA STORY 45.8 L'A RENDUE CERTAINE en donnant un producteur automatique a la
-- table : `context_review.request_review` est appele avec `requested_by` egal a
-- la constante GLOBALE `AGENT_AUTHOR` ("agent:toorow"), sur des noeuds du Hub a
-- portee plateforme -- lesquels n'ont pas d'`org_id` du tout et portent
-- `project_id IS NULL`. Deux organisations qui rencontrent le meme trou de
-- contexte produisent alors, mot pour mot, la meme cle.
--
-- CE QUE CA FAISAIT, MESURE CONTRE UNE BASE JETABLE (2026-08-05) :
--
--     A filed -> crr_01KZ922RVVT57ZX5F66W47CS1R  org_TENANT_A
--     B filed -> crr_01KZ922RVVT57ZX5F66W47CS1R  org_TENANT_A
--     same row: True | B queue: [] | rows in table: 1
--
-- Les deux defauts a la fois, et le second est le pire. L'organisation B REND
-- LA LIGNE DE L'ORGANISATION A -- l'INSERT ne mordait pas, le repli
-- re-SELECTait sans predicat de locataire, et la ligne etrangere repartait dans
-- le corps du 201. Et la remarque de B n'etait JAMAIS deposee : le producteur se
-- declenche sur `== RECURRENCE_THRESHOLD`, une egalite et non un seuil franchi,
-- donc il n'y a pas de second passage qui la rattrape.
--
-- COALESCE PLUTOT QUE LA COLONNE NUE. `project_id` est nullable, et dans un
-- index unique Postgres deux NULL ne sont pas egaux : ajouter `project_id` tel
-- quel aurait rendu la DEDUPLICATION INOPERANTE pour tout noeud a portee
-- plateforme -- c'est-a-dire exactement le cas que cette migration existe pour
-- reparer, retourne a l'envers. Le COALESCE fait que deux remarques de portee
-- plateforme dans la meme organisation restent un doublon.
--
-- LA 202 N'EST PAS RE-EDITEE. Elle est appliquee et scellee dans le manifeste ;
-- on corrige par la suivante. L'ancien index est DROP puis remplace, dans la
-- meme transaction, pour qu'aucune fenetre ne laisse la table sans garde de
-- doublon.

DROP INDEX IF EXISTS app.uq_context_review_request_open;

CREATE UNIQUE INDEX IF NOT EXISTS uq_context_review_request_open
    ON app.context_review_requests
       (org_id, COALESCE(project_id, ''), node_type, node_id, node_version,
        requested_by, md5(note))
    WHERE status = 'open';
