-- 300 -- Un attribut se lit A LA DATE DE LA LIGNE, pas a celle du lecteur.
--
-- CE QUI MANQUAIT. `app.master_data_node_attributes_dim_v` (migration 241) rend
-- les attributs de la version COURANTE d'un noeud, et rien d'autre. C'est ce
-- qu'il faut pour repondre << que sait-on de cette entite maintenant ? >>. Ce
-- n'est pas ce qu'il faut pour croiser un FAIT avec un attribut : une video
-- reclassee aujourd'hui rendrait sa nouvelle categorie a des jours ou elle ne
-- l'avait pas, et une lecture d'aout dernier changerait de reponse a chaque
-- reclassement. La regle est deja ecrite ailleurs -- `resolve_governed_node`
-- (`as_of_date`) : << une ligne d'un fait se resout a SA date, jamais a
-- aujourd'hui >>. Cette vue applique la meme regle aux attributs.
--
-- COMMENT LA FENETRE EST CALCULEE, ET POURQUOI ELLE N'EST PAS STOCKEE. Une
-- version porte son `effective_date` ; la version SUIVANTE du meme noeud
-- ouvre la fenetre suivante. `LEAD(...)` la calcule -- une valeur derivable ne
-- se stocke pas (CLAUDE.md), et la stocker exigerait de reecrire la ligne
-- precedente a chaque nouvelle version, sur une table append-only.
--
-- TOUTES LES VERSIONS, PAS SEULEMENT LA COURANTE. C'est le point : une lecture
-- as-of a besoin de l'historique. Les versions `draft` sont exclues -- elles
-- n'ont ete publiees a aucune date, donc elles n'ont jamais fait foi.
--
-- ---------------------------------------------------------------------------
-- ET LES ATTRIBUTS DERIVES (story 68.6).
--
-- Ils ne vivent pas dans le payload d'un noeud : une regle publiee les calcule
-- et les estampille de SA version (`master_data_derived_attributes`, migration
-- 294). Ils n'etaient pas mirroites, donc l'entrepot ne pouvait pas croiser
-- par une classification de l'utilisateur -- ce que l'epic 68 existe pour
-- rendre possible.
--
-- LEUR AS-OF N'EST PAS UNE DATE, C'EST UNE VERSION DE REGLE, et c'est une
-- difference de nature, pas de forme. Un attribut porte affirme << au 3 aout,
-- cette video durait 42 s >> ; un attribut derive affirme << sous la regle v2,
-- cette video est courte >>. Republier une regle NE reecrit aucun fait : il
-- naît des lignes portant la nouvelle version, les anciennes restent
-- interrogeables, et une reponse doit NOMMER la version qu'elle a lue. La vue
-- rend donc les deux : la ligne, et la version qui l'a produite.
--
-- Cette migration ne cree ni table ni colonne : deux vues. Rien a retro-remplir.
--
-- Contrat : story 69.3, docs/product-architecture/data.md.

BEGIN;

CREATE OR REPLACE VIEW app.master_data_node_attributes_asof_v AS
-- LA PORTEE PROJET VIENT DU REGISTRE, PAS DE LA VERSION. `create_node_version`
-- insere `project_id` a NULL DELIBEREMENT (une version de noeud est scopee par
-- son registre, qui porte le projet) -- lire la colonne de la version rendrait
-- une portee vide, et toute jointure sur elle ne rattacherait rien.
WITH windows AS (
    SELECT v.org_id,
           v.registry_id,
           v.node_id,
           v.id            AS version_id,
           v.version_number,
           v.effective_date AS effective_from,
           LEAD(v.effective_date) OVER (
               PARTITION BY v.node_id ORDER BY v.effective_date, v.version_number
           )               AS effective_to,
           v.payload
      FROM app.master_data_object_versions v
     WHERE v.node_id IS NOT NULL
       AND v.status <> 'draft'
       AND v.effective_date IS NOT NULL
),
carried AS (
    SELECT w.org_id,
           w.registry_id,
           w.node_id,
           w.version_id,
           w.effective_from,
           w.effective_to,
           a.key   AS attribute,
           a.value AS raw
      FROM windows w
      CROSS JOIN LATERAL jsonb_each(
          COALESCE(w.payload -> 'attributes', '{}'::jsonb)
      ) AS a(key, value)
)
SELECT c.org_id,
       r.project_id,
       c.registry_id,
       c.node_id,
       c.version_id,
       c.effective_from,
       c.effective_to,
       r.object_kind,
       c.attribute,
       CASE WHEN jsonb_typeof(c.raw) = 'string' THEN c.raw #>> '{}'
            ELSE c.raw::text END                       AS value_text,
       CASE WHEN jsonb_typeof(c.raw) = 'number' THEN (c.raw #>> '{}')::NUMERIC END
                                                       AS value_number
  FROM carried c
  JOIN app.master_data_registries r
    ON r.id = c.registry_id;

COMMENT ON VIEW app.master_data_node_attributes_asof_v IS
    'Les attributs PORTES d un noeud, avec la fenetre ou chacun faisait foi '
    '[effective_from, effective_to). La vue 241 rend la version courante -- la '
    'bonne reponse a << que sait-on maintenant >>, la mauvaise a << que '
    'savait-on le 3 aout >>. Une ligne d un fait se croise a SA date (story '
    '69.3), la meme regle que resolve_governed_node applique deja aux alias.';


CREATE OR REPLACE VIEW app.master_data_derived_attributes_dim_v AS
SELECT d.org_id,
       d.project_id,
       d.registry_id,
       d.node_id,
       r.object_kind,
       d.attribute,
       CASE WHEN jsonb_typeof(d.value) = 'string' THEN d.value #>> '{}'
            ELSE d.value::text END                     AS value_text,
       CASE WHEN jsonb_typeof(d.value) = 'number' THEN (d.value #>> '{}')::NUMERIC END
                                                       AS value_number,
       d.rule_set_id,
       d.rule_set_version_id,
       (g.current_version_id = d.rule_set_version_id)  AS is_current_rule_version
  FROM app.master_data_derived_attributes d
  JOIN app.master_data_registries r
    ON r.id = d.registry_id AND r.project_id = d.project_id
  LEFT JOIN app.governance_rule_sets g
    ON g.id = d.rule_set_id;

COMMENT ON VIEW app.master_data_derived_attributes_dim_v IS
    'Les classifications que les REGLES de l utilisateur derivent (story 68.6), '
    'estampillees de la version de regle qui les a produites. Republier une '
    'regle ne reecrit aucun fait : les lignes de l ancienne version restent '
    'interrogeables, et une reponse doit NOMMER celle qu elle a lue -- '
    '`is_current_rule_version` dit laquelle fait foi aujourd hui.';

-- ---------------------------------------------------------------------------
-- ET LE VERDICT QUI RATTACHE UNE CLE A UN NOEUD (story 68.3).
--
-- POURQUOI LE MIROIR PLUTOT QU'UN SECOND RESOLVEUR. `resolve_governed_node`
-- resout un mot dans un NAMESPACE ; la story 68.3 resout une occurrence d'une
-- cle DESIGNEE, et se scope sur le REGISTRE du type declare, pas sur un
-- namespace ("la designation nomme le TYPE dont la valeur est une cle"). Les
-- deux repondent a la meme question sous deux autorites. En ecrire une
-- troisieme dans une vue semantique donnerait un jour deux reponses
-- differentes a << que designe cette valeur ? >>, et rien ne dirait laquelle
-- fait foi. Le croisement LIT donc le verdict que 68.3 a deja rendu.
--
-- SEULEMENT LES LIGNES `current`. Une supersedee est de l'histoire, pas une
-- reponse ; les deux ensemble rendraient deux noeuds pour une valeur.

CREATE OR REPLACE VIEW app.entity_key_match_verdicts_dim_v AS
SELECT v.org_id,
       v.project_id,
       v.datastream_id,
       v.object_kind,
       v.field_id,
       v.normalized_value,
       v.node_id,
       v.verdict,
       v.reason_code,
       v.observed_from,
       v.observed_to
  FROM app.entity_key_match_verdicts v
 WHERE v.state = 'current';

COMMENT ON VIEW app.entity_key_match_verdicts_dim_v IS
    'Le verdict de rattachement d une occurrence de cle designee (story 68.3), '
    'ligne courante seulement. Mirroite pour que la lecture croisee (69.3) LISE '
    'ce rattachement au lieu d en ecrire un second -- deux resolveurs finissent '
    'par rendre deux reponses, et rien ne dit laquelle fait foi.';

COMMIT;
