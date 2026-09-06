{#-
  CROISER UN FAIT AVEC UNE CLASSIFICATION DE L'UTILISATEUR, A LA DATE DE LA LIGNE.

  CE QUE CETTE VUE REND POSSIBLE (story 69.3, FR8). Un fait porte une cle
  d'entite dans son axe (`breakdown_dimension` / `breakdown_value` -- un
  `video_id`, un `store_id`). L'utilisateur, lui, raisonne dans SES mots :
  << les vues par type de contenu >>, << le cout par marche >>. Le pont entre
  les deux est le MDM : la cle nomme un noeud, le noeud porte des attributs, et
  des regles versionnees en derivent des classifications. Rien de tout cela
  n'etait joignable depuis un fait.

  L'ATTRIBUT NE DESCEND JAMAIS DANS LE MART. C'est la regle de l'epic (FR8), et
  ce n'est pas une preference : denormaliser `content_type` dans
  `fact_daily_kpi` ferait qu'un reclassement REECRIT l'histoire -- les chiffres
  d'aout changeraient parce qu'on a range une video autrement en octobre. Le
  croisement est une LECTURE, et il vit ici.

  LE RATTACHEMENT EST LU, PAS RE-CALCULE. La story 68.3 rend deja un verdict par
  occurrence de cle designee -- `resolved` / `unmatched` / `ambiguous`, avec son
  noeud et son evidence -- sous l'autorite du REGISTRE du type declare. Ecrire
  ici un second resolveur (`resolve_governed_node`, qui se scope par NAMESPACE)
  donnerait un jour deux reponses differentes a << que designe cette valeur ? >>
  sans que rien ne dise laquelle fait foi. Cette vue joint le verdict.

  L'AS-OF PORTE SUR CE QUI VARIE DANS LE TEMPS -- L'ATTRIBUT :

    * le rattachement cle -> noeud est le verdict COURANT (68.3 supersede par
      nouvelle ligne : << que designe cette valeur, ici, maintenant >> a une
      seule reponse) ;
    * l'attribut PORTE se lit dans la fenetre `[effective_from, effective_to)`
      de la version qui faisait foi CE JOUR-LA (migration 300). La vue 241 rend
      la version courante, ce qui repondrait << ce que l'on sait maintenant >> a
      une question posee sur aout ;
    * la classification DERIVEE est estampillee de la version de REGLE qui l'a
      produite (68.6). Son as-of n'est pas une date mais une version, et la
      difference est de nature : << au 3 aout la video durait 42 s >> n'est pas
      << sous la regle v2 la video est courte >>. La vue rend la version, et le
      lecteur la NOMME dans sa reponse.

  CE QUI N'EST PAS RATTACHE EST NOMME, JAMAIS JETE (AD-9). Une ligne dont la cle
  ne resout aucun noeud -- ou dont le noeud ne porte aucun attribut ce jour-la --
  garde sa valeur et rejoint un groupe qui DIT pourquoi : `resolution_state`
  porte le mot de la taxonomie ratifiee et `attribute_value` reste NULL.
  Additionner ce groupe dans un vrai libelle serait un total faux ; le laisser
  tomber ferait un total incomplet qui se lit comme complet.

  UNE VUE, PAS UNE TABLE. Un croisement se lit ; le materialiser stockerait une
  reponse que le prochain reclassement rendrait fausse sans que rien ne le dise.
-#}
{#- `ref()` vit sous condition (la branche vide quand le miroir est en retard),
    donc dbt ne peut pas inferer la dependance statiquement : elle est declaree
    ici. Le saut de ligne apres ce commentaire doit SURVIVRE. -#}
-- depends_on: {{ ref('fact_daily_kpi') }}
{{ config(materialized='view') }}

{#- Le miroir peut ne pas avoir tourne depuis la migration 300 : on INTERROGE
    les relations plutot que de les supposer. Une synchro en retard rend une vue
    VIDE mais valide -- jamais un build mort. -#}
{%- set needed = [
      'entity_key_match_verdicts_dim',
      'master_data_node_attributes_asof',
      'master_data_derived_attributes_dim',
    ] -%}
{%- set missing = [] -%}
{%- for name in needed -%}
  {%- set relation = adapter.get_relation(
        database=source('mirror', name).database,
        schema=source('mirror', name).schema,
        identifier=source('mirror', name).identifier) -%}
  {%- if relation is none -%}
    {%- do missing.append(name) -%}
  {%- endif -%}
{%- endfor -%}

{%- if execute and missing | length > 0 -%}
  {{ log("semantic_fact_by_entity_attribute: miroir incomplet (" ~ (missing | join(', '))
        ~ ") -- la vue est vide, resynchroniser le miroir apres la migration 300",
        info=true) }}
  {#- ET LE MARQUEUR QUE LE NOCTURNE LIT (AI-314). Sans lui, un projet dont tous
      les marts gouvernes sont vides parce que le miroir n'a pas ete ecrit rend
      << ok >> : dbt a bien bati des modeles. Le verdict se lit sur ce mot-la. -#}
  {{ toorow_log_source_absent('mirror', missing) }}
{%- endif -%}

{%- if missing | length > 0 -%}

SELECT
    CAST(NULL AS {{ dbt.type_string() }}) AS project_id,
    CAST(NULL AS DATE)    AS date,
    CAST(NULL AS {{ dbt.type_string() }}) AS connector,
    CAST(NULL AS {{ dbt.type_string() }}) AS metric,
    CAST(NULL AS {{ dbt.type_string() }}) AS entity_dimension,
    CAST(NULL AS {{ dbt.type_string() }}) AS entity_key,
    CAST(NULL AS {{ dbt.type_string() }}) AS object_kind,
    CAST(NULL AS {{ dbt.type_string() }}) AS node_id,
    CAST(NULL AS {{ dbt.type_string() }}) AS resolution_state,
    CAST(NULL AS {{ dbt.type_string() }}) AS attribute,
    CAST(NULL AS {{ dbt.type_string() }}) AS attribute_value,
    CAST(NULL AS {{ dbt.type_string() }}) AS attribute_origin,
    CAST(NULL AS {{ dbt.type_string() }}) AS rule_set_version_id,
    CAST(NULL AS {{ dbt.type_float() }})  AS value,
    CAST(NULL AS {{ dbt.type_string() }}) AS pull_id
-- PORTABLE EMPTY SET: `WHERE FALSE` needs something to filter. BigQuery refuses
-- a WHERE on a query with no FROM -- `Query without FROM clause cannot have a
-- WHERE clause` -- while DuckDB accepts it, so this branch built green locally
-- and could not compile on the engine production runs. Measured 2026-08-24 by
-- replaying the nightly build of a project whose sources are absent, which is
-- the only case that reaches this branch. `FROM (SELECT 1)` gives the filter a
-- row to reject, and both engines accept it.
FROM (SELECT 1) AS _empty
WHERE FALSE

{%- else -%}

WITH facts AS (
    SELECT
        f.project_id,
        {#- AI-303 : LE JOUR EST UNE DATE ICI, ET IL NE L'ETAIT PAS. La fenetre
            `[effective_from, effective_to)` plus bas compare ce jour a deux
            colonnes DATE du miroir -- c'est ce que `mirror_sync._PG_TO_DUCKDB`
            ecrit pour un `date` Postgres. `fact_daily_kpi.date` est l'UNION de
            trente branches de connecteur et retombe sur VARCHAR, donc la
            comparaison echouait au build :

              Binder Error: Cannot compare values of type DATE and type VARCHAR

            La conformance ne pouvait pas le voir : elle bat un graphe REDUIT
            (`--select` une lignee de quatre modeles), ou le fait ne porte que la
            branche managed_feed et garde son type de date. Un entrepot complet
            est la seule forme ou l'union existe -- et c'est la forme de la
            production. Le CAST est donc explicite, et juste dans les deux. -#}
        CAST(f.date AS DATE) AS date,
        f.connector,
        f.metric,
        f.breakdown_dimension AS entity_dimension,
        f.breakdown_value     AS entity_key,
        f.value,
        f.pull_id
    FROM {{ ref('fact_daily_kpi') }} AS f
    -- `day_total` ne nomme aucune entite : un total de la journee n'a pas de
    -- cle a resoudre, et le croiser produirait un << non rattache >> qui
    -- n'apprend rien a personne.
    WHERE f.breakdown_dimension <> 'day_total'
      AND f.breakdown_value IS NOT NULL
),

{#- LE RATTACHEMENT, LU. La normalisation est celle de `governed_alias_normalized`
    -- le jumeau SQL de `normalize_alias_value`, la seule autorite -- parce que
    c'est sous cette forme que 68.3 a persiste la valeur. -#}
resolved AS (
    SELECT
        facts.*,
        v.node_id,
        v.object_kind,
        COALESCE(v.verdict, 'unknown') AS resolution_state
    FROM facts
    LEFT JOIN {{ source('mirror', 'entity_key_match_verdicts_dim') }} AS v
      ON v.project_id = facts.project_id
     AND v.field_id = facts.entity_dimension
     AND v.normalized_value = {{ governed_alias_normalized("facts.entity_key") }}
),

carried AS (
    SELECT
        a.project_id,
        a.node_id,
        a.attribute,
        a.effective_from,
        a.effective_to,
        COALESCE(a.value_text, CAST(a.value_number AS {{ toorow_string_type() }})) AS attribute_value
    FROM {{ source('mirror', 'master_data_node_attributes_asof') }} AS a
),
derived AS (
    SELECT
        d.project_id,
        d.node_id,
        d.attribute,
        COALESCE(d.value_text, CAST(d.value_number AS {{ toorow_string_type() }})) AS attribute_value,
        d.rule_set_version_id
    FROM {{ source('mirror', 'master_data_derived_attributes_dim') }} AS d
    -- La version de regle qui FAIT FOI aujourd'hui. Les autres restent en base
    -- et restent interrogeables ; les melanger ici rendrait deux valeurs pour
    -- une entite et un attribut.
    WHERE d.is_current_rule_version
),

crossed AS (
    {# LES DEUX ORIGINES D'UN ATTRIBUT, UNIES ET DISTINGUEES. `attribute_origin`
        dit LAQUELLE a repondu : un attribut porte par le fichier de
        l'utilisateur et une classification que ses regles derivent ne
        s'argumentent pas de la meme facon, et une reponse qui les confondrait
        empecherait de savoir quoi corriger. #}
    SELECT
        r.project_id,
        r.date,
        r.connector,
        r.metric,
        r.entity_dimension,
        r.entity_key,
        r.object_kind,
        r.node_id,
        r.resolution_state,
        c.attribute,
        c.attribute_value,
        'carried'             AS attribute_origin,
        CAST(NULL AS {{ dbt.type_string() }}) AS rule_set_version_id,
        r.value,
        r.pull_id
    FROM resolved AS r
    JOIN carried AS c
      ON c.node_id = r.node_id
     AND c.project_id = r.project_id
     -- LA FENETRE, PAS LA VERSION COURANTE : la ligne lit ce qui faisait foi
     -- CE JOUR-LA.
     AND c.effective_from <= r.date
     AND (c.effective_to IS NULL OR c.effective_to > r.date)

    UNION ALL

    SELECT
        r.project_id,
        r.date,
        r.connector,
        r.metric,
        r.entity_dimension,
        r.entity_key,
        r.object_kind,
        r.node_id,
        r.resolution_state,
        d.attribute,
        d.attribute_value,
        'derived'            AS attribute_origin,
        d.rule_set_version_id,
        r.value,
        r.pull_id
    FROM resolved AS r
    JOIN derived AS d
      ON d.node_id = r.node_id
     AND d.project_id = r.project_id

    UNION ALL

    {# LE GROUPE << NON RATTACHE >>, ET IL EST UNE LIGNE. Une ligne dont la cle
        ne resout rien -- ou dont le noeud ne porte aucun attribut ce jour-la --
        garde sa valeur, son `resolution_state` et un `attribute_value` NULL.
        L'omettre ferait un total incomplet qui se lit comme complet ; la ranger
        dans un vrai libelle ferait un total faux. #}
    SELECT
        r.project_id,
        r.date,
        r.connector,
        r.metric,
        r.entity_dimension,
        r.entity_key,
        r.object_kind,
        r.node_id,
        r.resolution_state,
        CAST(NULL AS {{ dbt.type_string() }}) AS attribute,
        CAST(NULL AS {{ dbt.type_string() }}) AS attribute_value,
        'unattached'          AS attribute_origin,
        CAST(NULL AS {{ dbt.type_string() }}) AS rule_set_version_id,
        r.value,
        r.pull_id
    FROM resolved AS r
    WHERE r.node_id IS NULL
       OR (
            NOT EXISTS (
                SELECT 1 FROM carried AS c
                 WHERE c.node_id = r.node_id AND c.project_id = r.project_id
                   AND c.effective_from <= r.date
                   AND (c.effective_to IS NULL OR c.effective_to > r.date)
            )
            AND NOT EXISTS (
                SELECT 1 FROM derived AS d
                 WHERE d.node_id = r.node_id AND d.project_id = r.project_id
            )
          )
)

SELECT * FROM crossed

{%- endif -%}
