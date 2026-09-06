{#-
  Quel chargement gagne, pour chaque cle d'un flux fichier.

  LE PROBLEME. Renvoyer un fichier qui recouvre des jours deja recus empile les
  lignes : le brut est ajout-seul et rien ne les supersede. Les 52 modeles de
  staging des connecteurs resolvent cela en lecture --
  `QUALIFY ROW_NUMBER() OVER (PARTITION BY <maille> ORDER BY pull_id DESC) = 1`
  -- et le chemin fichier n'avait aucun modele.

  POURQUOI CE MODELE EST UN INDEX ET PAS UNE TABLE DE DONNEES. Chaque flux
  fichier a SES colonnes : elles viennent du fichier de l'operateur. Deux flux
  n'ont donc pas la meme forme et ne peuvent pas etre unis. Ce qui EST uniforme,
  c'est la question posee -- << pour cette cle, quel chargement fait foi ? >>.
  Le modele repond cela, et la donnee se joint dessus par
  (datastream_id, execution_id) plus les colonnes de maille. Pretendre normaliser
  des fichiers heterogenes produirait une table qui ment sur la moitie d'entre eux.

  LA MAILLE VIENT DU MAPPING PUBLIE, mirroitee par `mirror.managed_feed_grain`.
  Un flux dont `has_grain` est FALSE n'a pas encore de cle : il est EXCLU du
  superseding et compte parmi `streams_without_grain` ci-dessous, parce qu'un flux
  omis en silence se lirait comme un flux sans probleme alors qu'il est celui dont
  les doublons ne peuvent pas etre resolus.

  L'ORDRE EST `execution_id DESC`. Les lignes d'un import portent leur
  `execution_id` (colonnes de provenance de `csv_excel_import`) et c'est un
  `dse_<ULID>` : monotone, donc le plus grand est le plus recent. C'est la meme
  propriete que `pull_id DESC` chez les connecteurs, sous un autre nom parce que
  ce n'est pas un pull.
-#}

{%- set streams = [] -%}
{%- set streams_without_grain = [] -%}
{#- AI-303 : LES FLUX DONT L ATTERRISSAGE N EXISTE PAS DANS L ENTREPOT. Ce n est
    pas un cas de bord, c est la forme NORMALE d un projet d epine d entites : un
    catalogue prend la route reference (ses lignes deviennent des attributs MDM en
    Postgres) et un calendrier la route evenements (`app.context_events`). Ni l un
    ni l autre n ecrit une relation d entrepot -- et `app.managed_feed_grain_v` les
    liste quand meme, avec `has_grain` vrai et un `landing_table` que personne n a
    jamais cree.
    Ce modele les lisait sans demander si la relation existe, donc un projet
    portant un catalogue ET un fichier de faits faisait ECHOUER le build entier :
    << Catalog Error: Table with name managed_feed_ds_… does not exist >>. Mesure
    du 2026-08-23, sur le lot de semences capture de la chaine des epics 68-69 :
    trois lignes de maille, une seule relation ecrite.
    Le voisin `stg_managed_feed_facts` interroge deja chaque atterrissage et tient
    la liste de ceux qui manquent. Deux lecteurs du meme miroir, un seul se
    protegeait. -#}
{%- set streams_without_landing = [] -%}

{#- Le miroir peut ne pas avoir encore tourne (entrepot neuf, CI sans Postgres).
    On INTERROGE la relation plutot que de la supposer : un modele qui casse le
    build entier parce qu'une synchro n'a pas encore eu lieu transforme une
    absence ordinaire en panne. -#}
{%- set grain_relation = adapter.get_relation(
      database=source('mirror', 'managed_feed_grain').database,
      schema=source('mirror', 'managed_feed_grain').schema,
      identifier=source('mirror', 'managed_feed_grain').identifier) -%}

{#- ET LE MARQUEUR QUE LE NOCTURNE LIT (AI-314) : un modele bati VIDE parce que
    le miroir n'existe pas dans cet entrepot doit le DIRE, sinon le projet rend
    << ok >> et personne ne sait que rien de gouverne n'a ete bati. -#}
{%- if execute and grain_relation is none -%}
  {{ toorow_log_source_absent('mirror', ['managed_feed_grain']) }}
{%- endif -%}

{%- if execute and grain_relation is not none -%}
  {%- set grain_rows = run_query(
        "SELECT datastream_id, project_id, landing_table, grain_columns, has_grain "
        "FROM " ~ grain_relation
      ) -%}
  {%- for row in grain_rows.rows -%}
    {%- if row['has_grain'] -%}
      {%- set landing = adapter.get_relation(
            database=target.database,
            schema=target.schema,
            identifier=row['landing_table']) -%}
      {%- if landing is not none -%}
        {%- set columns = fromjson(row['grain_columns'] | string) -%}
        {%- do streams.append({
              'datastream_id': row['datastream_id'],
              'project_id': row['project_id'],
              'table': row['landing_table'],
              'grain': columns,
            }) -%}
      {%- else -%}
        {%- do streams_without_landing.append(row['datastream_id']) -%}
      {%- endif -%}
    {%- else -%}
      {%- do streams_without_grain.append(row['datastream_id']) -%}
    {%- endif -%}
  {%- endfor -%}
{%- endif -%}

{#- Flux sans maille, nommes plutot que tus : {{ streams_without_grain | join(', ') }} -#}
{#- Flux sans atterrissage d entrepot, nommes de la meme facon (AI-303) :
    {{ streams_without_landing | join(', ') }} -#}
{%- if execute and streams_without_landing | length > 0 -%}
  {{ log("managed_feed_superseding: " ~ (streams_without_landing | length)
        ~ " flux declarent une maille sans relation d atterrissage ("
        ~ (streams_without_landing | join(', '))
        ~ ") -- routes reference/evenements, rien a superseder dans l entrepot",
        info=true) }}
{%- endif -%}

{%- if streams | length == 0 -%}

{#- Aucun flux fichier ne porte encore de maille publiee. Le modele existe, il
    est vide, et il le dit avec ses colonnes plutot qu'en echouant : un entrepot
    neuf n'est pas une panne.

    CETTE BRANCHE NE POUVAIT PAS COMPILER EN BIGQUERY, et c'est la SEULE que la
    production atteint (le miroir n'y est pas ecrit) : `WHERE 1 = 0` sans FROM
    est refuse -- *Query without FROM clause cannot have a WHERE clause* --
    exactement le defaut repare le 2026-08-24 dans deux autres marts. La forme
    portable vit desormais dans UN macro
    (`toorow_empty_projection`, dbt/macros/relation_present.sql). -#}
{{ toorow_empty_projection([
    ['project_id', 'string'],
    ['datastream_id', 'string'],
    ['grain_key', 'string'],
    ['winning_execution_id', 'string'],
    ['superseded_loads', 'bigint'],
]) }}

{%- else -%}

{%- for stream in streams %}
{#- LE SEPARATEUR EST EXPLICITE ET NE PEUT PAS APPARAITRE DANS UNE VALEUR.
    Concatener sans separateur ferait collisionner ('ab','c') et ('a','bc') :
    deux mailles differentes rendues identiques, donc une ligne qui supersede
    une autre qu'elle ne recouvre pas. CHR(31) est le separateur d'unite ; il ne
    traverse ni un CSV ni une feuille de calcul, donc aucune valeur ne le porte.
    Chaque colonne est COALESCE'e : en SQL `NULL || x` vaut NULL, et une cle NULL
    regrouperait ensemble tous les jours dont une dimension est vide. -#}
{%- set parts = [] -%}
{%- for column in stream['grain'] -%}
  {%- do parts.append("COALESCE(CAST(" ~ (column | string | replace('\"', '')) ~ " AS " ~ toorow_string_type() ~ "), '')") -%}
{%- endfor -%}
{%- set key_expression = parts | join(' || CHR(31) || ') -%}
SELECT
    '{{ stream['project_id'] }}'    AS project_id,
    '{{ stream['datastream_id'] }}' AS datastream_id,
    {{ key_expression }}            AS grain_key,
    MAX(execution_id)               AS winning_execution_id,
    -- Combien de chargements ont couvert cette cle. 1 = jamais recouverte ;
    -- au-dela, c'est exactement ce que ce modele supersede, et le nombre est la
    -- mesure de ce qu'une lecture naive aurait compte en double.
    COUNT(DISTINCT execution_id)    AS superseded_loads
FROM {{ target.schema }}.{{ stream['table'] }}
GROUP BY {{ key_expression }}
{#- LE SAUT DE LIGNE APRES `UNION ALL` DOIT SURVIVRE. Un `{%- endif %}` ici
    avalerait la fin de ligne et rendrait `UNION ALLSELECT` -- du SQL invalide,
    invisible tant qu'un seul flux existe (une seule branche n'a pas de
    separateur a rendre). Mesure story 69.2, sur une fixture a trois flux. -#}
{%- if not loop.last %}
UNION ALL
{% endif %}
{%- endfor %}

{%- endif -%}
