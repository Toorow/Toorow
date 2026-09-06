-- depends_on: {{ ref('managed_feed_superseding') }}
{#-
  STAGING MANAGED FEED -- les faits fichier qui font foi, et rien d'autre.

  LE CONSOMMATEUR DU SUPERSEDING (story 69.1, audit R1). Le mart
  `managed_feed_superseding` repond a << pour cette cle, quel chargement fait
  foi ? >> et n'avait aucun consommateur. Ce staging est ce consommateur : il
  JOINT chaque landing `managed_feed_<ds>` sur l'index et ne garde que les
  lignes du chargement gagnant -- l'analogue du
  `QUALIFY ROW_NUMBER() ... ORDER BY pull_id DESC = 1` des stagings
  connecteurs, avec `execution_id` (un dse_<ULID> monotone) pour `pull_id`.

  LA MAILLE ET LA CLE VIENNENT DU MAPPING EPINGLE, mirroitees par
  `mirror.managed_feed_grain` (migration 227) : `grain_columns` est la liste
  de colonnes de la version de mapping PUBLIEE -- des noms ATTERRIS depuis la
  migration 302, pas les `field_id` sources : ce modele les concatene CONTRE la
  relation atterrie, donc une maille dont le mapping renomme une colonne (`day`
  -> `date`, le cas ordinaire d'une date) nommait une colonne inexistante et
  faisait echouer le build. Le nom source reste lisible sous `grain_field_ids`.
  L'expression de cle est
  reproduite A L'IDENTIQUE du mart (COALESCE + CHR(31)) -- une cle composee
  autrement ne joindrait jamais. Un flux sans maille publiee (has_grain
  FALSE) est exclu, comme dans le mart : il ne peut pas etre dedoublonne, et
  l'inclure en silence avec ses doublons le lirait comme un flux sain.

  D'OU VIENNENT LES COLONNES PROJETEES. La maille (le miroir) et la
  provenance (le contrat du writer) sont nommees explicitement. Les colonnes
  de donnee sont lues dans la relation de landing a la compilation : le
  pipeline GARANTIT que la forme d'une landing est une projection epinglee --
  `_apply_governed_mapping` ne projette que les cibles mappees (+ `grain_key`,
  colonne reservee), et `promote_candidate` refuse toute promotion dont le
  schema differe de la table existante. Une colonne rejetee par le mapping
  epingle n'atterrit donc jamais et ne peut pas atteindre ce staging. Limite
  connue : si le mapping PUBLIE change apres la creation de la table (la
  forme physique reste celle de l'ancienne version, toute promotion sous la
  nouvelle echouant au controle de schema), une colonne epinglee par
  l'ANCIENNE version resterait visible ici. Fermer ce trou demande de
  mirroiter la liste complete des champs epingles (migration, hors perimetre
  de cette story).

  UNION LARGE, JAMAIS DE LONG FORMAT. Deux flux n'ont pas les memes colonnes
  et ne peuvent pas pretendre a une forme commune (le mart le dit deja) : le
  staging fait l'UNION des colonnes de donnee -- chaque colonne absente d'un
  flux y est un NULL type -- a la maniere des relations full-grain de la
  maison (`candidate_full_grain`) : chaque dimension garde SA colonne typee,
  jamais une paire breakdown_dimension/breakdown_value. Un conflit de type
  sur un meme nom entre flux retombe sur VARCHAR.

  PROVENANCE. `execution_id`, `plan_version_id`, `mapping_version_id` et
  `project_id` sont celles que le writer appose (import_runner /
  google_sheets_sync). `loaded_at` etait un NULL explicite : aucun chargement
  fichier ne tamponne l'heure sur SA LIGNE. C'etait vrai et c'etait le bon
  arbitrage tant que rien ne la demandait -- puis le mart l'a demandee non
  nulle sur toutes ses branches (story 69.2). La valeur existait pourtant :
  chaque execution porte son heure. Le staging JOINT donc
  `mirror.managed_feed_execution` (migration 299) sur `execution_id`. Rien
  n'est fabrique, et le NULL honnete reste le repli exact quand le miroir n'a
  pas encore tourne -- une fraicheur inconnue se lit comme un GAP, jamais
  comme maintenant (AD-9).
  `report_timezone` n'est pas traitee specialement : quand le mapping epingle
  une telle colonne, elle transite INCHANGEE comme toute colonne de donnee
  (capture only, contrat C6 -- jamais de convert_timezone au grain jour).

  PROJET SANS MANAGED FEED. Le modele se construit VIDE, avec ses colonnes
  fixes, sans erreur -- un entrepot sans flux fichier n'est pas une panne. Le
  scheduler n'exclut que les modeles des modules ; ce modele central participe
  donc a tout build par projet, et c'est cette forme vide qui porte
  l'honnetete du build (AC4).
-#}

{# Le premier tag ne porte pas de `-` d'ouverture : le saut de ligne apres le
   commentaire `depends_on` doit SURVIVRE, sinon il avale le SELECT rendu. #}
{% set streams = [] -%}
{%- set streams_without_grain = [] -%}
{%- set streams_without_landing = [] -%}
{%- set reserved = ['grain_key', 'execution_id', 'plan_version_id',
                    'mapping_version_id', 'project_id'] -%}

{#- Le miroir peut ne pas avoir encore tourne (entrepot neuf, CI sans
    Postgres). On INTERROGE la relation plutot que de la supposer -- meme
    garde que le mart : une synchro pas encore faite n'est pas une panne. -#}
{%- set grain_relation = adapter.get_relation(
      database=source('mirror', 'managed_feed_grain').database,
      schema=source('mirror', 'managed_feed_grain').schema,
      identifier=source('mirror', 'managed_feed_grain').identifier) -%}

{#- Le miroir des executions peut ne pas exister (miroir anterieur a la
    migration 299). On l'INTERROGE : son absence rend un `loaded_at` NULL, ce
    que le staging savait deja faire -- jamais une erreur de build. -#}
{%- set execution_relation = adapter.get_relation(
      database=source('mirror', 'managed_feed_execution').database,
      schema=source('mirror', 'managed_feed_execution').schema,
      identifier=source('mirror', 'managed_feed_execution').identifier) -%}

{#- LE MARQUEUR QUE LE NOCTURNE LIT (AI-314) : bati VIDE faute de miroir, dit. -#}
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
        {%- set landed = adapter.get_columns_in_relation(landing) -%}
        {%- set data_columns = [] -%}
        {%- for column in landed -%}
          {%- set name = column.name | string | replace('\"', '') -%}
          {%- if name not in reserved -%}
            {%- do data_columns.append((name, column.dtype | string)) -%}
          {%- endif -%}
        {%- endfor -%}
        {%- do streams.append({
              'datastream_id': row['datastream_id'],
              'project_id': row['project_id'],
              'table': row['landing_table'],
              'grain': fromjson(row['grain_columns'] | string),
              'data': data_columns,
            }) -%}
      {%- else -%}
        {%- do streams_without_landing.append(row['datastream_id']) -%}
      {%- endif -%}
    {%- else -%}
      {%- do streams_without_grain.append(row['datastream_id']) -%}
    {%- endif -%}
  {%- endfor -%}
{%- endif -%}

{#- L'UNION DES COLONNES DE DONNEE, ordonnee de facon deterministe (tri) :
    chaque branche rend exactement ces colonnes, dans cet ordre. Un meme nom
    type different selon les flux retombe sur VARCHAR. -#}
{%- set union_ns = namespace(names=[], dtypes={}) -%}
{%- for stream in streams -%}
  {%- for name, dtype in stream['data'] -%}
    {%- if name not in union_ns.dtypes -%}
      {%- do union_ns.dtypes.update({name: [dtype]}) -%}
      {%- do union_ns.names.append(name) -%}
    {%- elif dtype not in union_ns.dtypes[name] -%}
      {%- do union_ns.dtypes[name].append(dtype) -%}
    {%- endif -%}
  {%- endfor -%}
{%- endfor -%}
{%- set union_columns = union_ns.names | sort -%}
{%- set resolved = {} -%}
{%- for name in union_columns -%}
  {%- if union_ns.dtypes[name] | length == 1 -%}
    {%- do resolved.update({name: union_ns.dtypes[name][0]}) -%}
  {%- else -%}
    {%- do resolved.update({name: dbt.type_string() | string}) -%}
  {%- endif -%}
{%- endfor -%}

{#- Flux nommes plutot que tus : sans maille {{ streams_without_grain | join(', ') }} ;
    sans landing {{ streams_without_landing | join(', ') }} -#}
{%- if streams_without_grain | length > 0 -%}
  {{ log("stg_managed_feed_facts: flux sans maille publiee, exclus: "
        ~ (streams_without_grain | join(', ')), info=true) }}
{%- endif -%}
{%- if streams_without_landing | length > 0 -%}
  {{ log("stg_managed_feed_facts: flux sans relation de landing, exclus: "
        ~ (streams_without_landing | join(', ')), info=true) }}
{%- endif -%}

{%- if streams | length == 0 -%}

{#- Aucun flux fichier ne porte encore de maille publiee (ou le miroir n'a pas
    encore tourne). Le modele existe, il est vide, et il le dit avec ses
    colonnes fixes plutot qu'en echouant : un entrepot neuf n'est pas une panne.

    MEME DEFAUT DE DIALECTE QUE LE MART, MEME REPARATION (2026-08-24) : `WHERE
    1 = 0` sans FROM est refuse par BigQuery, et cette branche est la SEULE que
    la production atteint puisque le miroir n'y est pas ecrit. La forme portable
    vit dans `toorow_empty_projection` (dbt/macros/relation_present.sql). -#}
{{ toorow_empty_projection([
    ['project_id', 'string'],
    ['datastream_id', 'string'],
    ['grain_key', 'string'],
    ['execution_id', 'string'],
    ['plan_version_id', 'string'],
    ['mapping_version_id', 'string'],
    ['loaded_at', 'timestamp'],
]) }}

{%- else -%}

{%- for stream in streams %}
{#- L'EXPRESSION DE CLE EST CELLE DU MART, A L'IDENTIQUE (separateur CHR(31),
    COALESCE de chaque colonne) : c'est elle qui fait la jointure, une variante
    d'un caractere ne joindrait plus rien. -#}
{%- set parts = [] -%}
{%- for column in stream['grain'] -%}
  {%- do parts.append("COALESCE(CAST(l." ~ (column | string | replace('\"', '')) ~ " AS " ~ toorow_string_type() ~ "), '')") -%}
{%- endfor -%}
{%- set key_expression = parts | join(' || CHR(31) || ') -%}
{%- set own_columns = stream['data'] | map(attribute=0) | list -%}
SELECT
    l.project_id                            AS project_id,
    '{{ stream['datastream_id'] }}'         AS datastream_id,
    {%- for name in union_columns %}
    {%- if name in own_columns %}
    {%- if union_ns.dtypes[name] | length > 1 %}
    CAST(l.{{ name }} AS {{ resolved[name] }}) AS {{ name }},
    {%- else %}
    l.{{ name }}                            AS {{ name }},
    {%- endif %}
    {%- else %}
    CAST(NULL AS {{ resolved[name] }})      AS {{ name }},
    {%- endif %}
    {%- endfor %}
    s.grain_key                             AS grain_key,
    l.execution_id                          AS execution_id,
    l.plan_version_id                       AS plan_version_id,
    l.mapping_version_id                    AS mapping_version_id,
    {%- if execution_relation is not none %}
    -- L'heure du chargement, lue sur l'EXECUTION (migration 299) : la ligne de
    -- landing ne la porte pas, la course qui l'a posee si.
    CAST(x.loaded_at AS {{ dbt.type_timestamp() }}) AS loaded_at
    {%- else %}
    -- Miroir anterieur a la 299 : NULL honnete (AD-9). Une fraicheur inconnue
    -- se lit comme un GAP, jamais comme maintenant.
    CAST(NULL AS {{ dbt.type_timestamp() }}) AS loaded_at
    {%- endif %}
FROM {{ target.schema }}.{{ stream['table'] }} AS l
JOIN {{ ref('managed_feed_superseding') }} AS s
  ON s.datastream_id = '{{ stream['datastream_id'] }}'
 AND s.project_id = l.project_id
 AND s.winning_execution_id = l.execution_id
 AND s.grain_key = {{ key_expression }}
{%- if execution_relation is not none %}
LEFT JOIN {{ source('mirror', 'managed_feed_execution') }} AS x
  ON x.execution_id = l.execution_id
 AND x.project_id = l.project_id

 {# LE FLUX FAIT PARTIE DE LA CLE. Sans lui, un `execution_id` porte par deux
    flux du meme projet fait un fan-out et MULTIPLIE la valeur -- mesure sur la
    fixture a trois flux : 100 devenait 300. Les ULID sont uniques en pratique,
    ce qui rendrait le defaut invisible jusqu'au jour ou il ne l'est plus. #}
 AND x.datastream_id = '{{ stream['datastream_id'] }}'
{%- endif %}
{#- LE SAUT DE LIGNE APRES `UNION ALL` DOIT SURVIVRE. Un `{%- endif %}` ici
    avalerait la fin de ligne et rendrait `UNION ALLSELECT` -- du SQL invalide,
    invisible tant qu'un seul flux existe (une seule branche n'a pas de
    separateur a rendre). Mesure story 69.2, sur une fixture a trois flux. -#}
{%- if not loop.last %}
UNION ALL
{% endif %}
{%- endfor %}

{%- endif -%}
