-- 299 -- Un managed feed declare AUSSI ce que chacune de ses colonnes est.
--
-- POURQUOI, ET CE QUI MANQUAIT EXACTEMENT. La migration 227 mirroite la MAILLE
-- du mapping publie, et c'est tout ce que le superseding (story 69.1) avait
-- besoin de savoir : pour dedoublonner, il suffit de la cle. La branche
-- `fact_daily_kpi` (story 69.2) pose une autre question, a laquelle 227 ne
-- repond pas : parmi les colonnes qui atterrissent, LAQUELLE est le jour,
-- lesquelles sont des axes, lesquelles sont des mesures ? Sans reponse, un
-- modele ne peut que DEVINER -- typiquement << toute colonne numerique est une
-- mesure >>, ce qui ferait d'un identifiant de campagne numerique une somme.
--
-- LA REPONSE EXISTE DEJA, ELLE N'ETAIT PAS MIRROITEE. Chaque champ du
-- `mapping_payload` porte son `suggestion.semantic_role` (`primary_date`,
-- `dimension`, `measure`, `measure_spend`, `measure_revenue`), son
-- `suggestion.non_additive` et sa `binding.canonical_target`. Cette vue expose
-- ces trois faits, et rien de plus : elle ne classe pas, elle relaie une
-- declaration gouvernee.
--
-- LES NOMS SONT DES CIBLES CANONIQUES, PAS DES `field_id`. Ce qui atterrit
-- physiquement, c'est la projection epinglee -- `_apply_governed_mapping` ne
-- pose que les `canonical_target`. Une vue qui rendrait les `field_id` nommerait
-- des colonnes que la table de landing n'a pas des que le mapping renomme quoi
-- que ce soit. `COALESCE(canonical_target, field_id)` : la cible quand elle
-- existe, le nom source sinon (le cas par defaut, ou les deux coincident).
--
-- SEULES LES LIAISONS CONFIRMEES COMPTENT. Une colonne `excluded` ou encore
-- `suggested` n'atterrit pas ; l'annoncer comme mesure promettrait une valeur
-- qui n'arrive jamais. Meme lecture que `_apply_governed_mapping` et que
-- `reference_designation` (story 68.5) : `confirmed` ou `resolved`, et une
-- cible nommee.
--
-- UNE MESURE NON ADDITIVE EST NOMMEE, PAS OMISE (AD-4, AD-9). `measure_columns`
-- ne porte que l'additif -- une somme de taux est un mensonge, et le mart ne
-- doit jamais en stocker. Mais la colonne refusee est rendue dans
-- `non_additive_columns`, pour que le modele qui l'exclut puisse la NOMMER : une
-- colonne tue se lit comme une colonne absente du fichier.
--
-- `date_column` PEUT ETRE NULL, et c'est une reponse. Un fichier dont la maille
-- ne porte aucune date ne decrit pas des faits journaliers ; le mart doit
-- pouvoir dire << ce flux n'a pas de jour >> plutot que d'inventer une colonne.
--
-- Cette migration ne cree ni table ni colonne : elle REMPLACE une vue. Rien a
-- retro-remplir, rien a verrouiller.
--
-- Contrat : docs/product-architecture/data.md (managed feed), story 69.2.

BEGIN;

CREATE OR REPLACE VIEW app.managed_feed_grain_v AS
WITH published AS (
    SELECT
        d.project_id,
        d.org_id,
        d.id                                       AS datastream_id,
        'managed_feed_' || replace(d.id, '-', '_') AS landing_table,
        d.current_mapping_version_id               AS mapping_version_id,
        COALESCE(v.mapping_payload -> 'grain', '[]'::jsonb) AS grain_columns,
        v.mapping_payload                          AS mapping_payload
    FROM app.datastreams d
    LEFT JOIN app.datastream_mapping_versions v
           ON v.id = d.current_mapping_version_id
          AND v.project_id = d.project_id
    WHERE d.source_kind = 'managed_feed'
),
classified AS (
    SELECT
        p.datastream_id,
        f.value ->> 'field_id'                                     AS field_id,
        COALESCE(
            NULLIF(btrim(f.value -> 'binding' ->> 'canonical_target'), ''),
            f.value ->> 'field_id'
        )                                                          AS column_name,
        COALESCE(f.value -> 'suggestion' ->> 'semantic_role', '')  AS semantic_role,
        COALESCE(
            (f.value -> 'suggestion' ->> 'non_additive')::boolean, FALSE
        )                                                          AS non_additive,
        COALESCE(f.value -> 'binding' ->> 'status', '')            AS binding_status
    FROM published p
    CROSS JOIN LATERAL jsonb_array_elements(
        COALESCE(p.mapping_payload -> 'fields', '[]'::jsonb)
    ) AS f(value)
),
landed AS (
    SELECT * FROM classified
    WHERE binding_status IN ('confirmed', 'resolved')
      AND btrim(COALESCE(column_name, '')) <> ''
)
SELECT
    p.project_id,
    p.org_id,
    p.datastream_id,
    p.landing_table,
    p.mapping_version_id,
    p.grain_columns,
    jsonb_array_length(p.grain_columns) > 0 AS has_grain,
    -- The day this feed's rows are about. NULL is an answer: a file whose grain
    -- names no date does not describe daily facts.
    (
        SELECT l.column_name FROM landed l
        WHERE l.datastream_id = p.datastream_id
          AND l.semantic_role = 'primary_date'
          AND p.grain_columns ? l.field_id
        ORDER BY l.column_name
        LIMIT 1
    ) AS date_column,
    -- The axes: every grain column that is not the day.
    COALESCE((
        SELECT jsonb_agg(l.column_name ORDER BY l.column_name) FROM landed l
        WHERE l.datastream_id = p.datastream_id
          AND l.semantic_role = 'dimension'
          AND p.grain_columns ? l.field_id
    ), '[]'::jsonb) AS dimension_columns,
    -- The measures a mart may SUM. Additive only (AD-4).
    COALESCE((
        SELECT jsonb_agg(l.column_name ORDER BY l.column_name) FROM landed l
        WHERE l.datastream_id = p.datastream_id
          AND l.semantic_role LIKE 'measure%'
          AND NOT l.non_additive
    ), '[]'::jsonb) AS measure_columns,
    -- The measures a mart must REFUSE -- named so it can say so (AD-9).
    COALESCE((
        SELECT jsonb_agg(l.column_name ORDER BY l.column_name) FROM landed l
        WHERE l.datastream_id = p.datastream_id
          AND l.semantic_role LIKE 'measure%'
          AND l.non_additive
    ), '[]'::jsonb) AS non_additive_columns
FROM published p;

-- ---------------------------------------------------------------------------
-- ET QUAND CE CHARGEMENT A EU LIEU.
--
-- LE MART EXIGE UN `loaded_at` NON NUL, et le chemin fichier n'en avait pas :
-- la story 69.1 a mesure que ni `import_runner` ni `google_sheets_sync`
-- n'apposent d'heure de chargement sur les lignes qui atterrissent, et a rendu
-- un NULL honnete plutot qu'une valeur fabriquee. C'etait le bon arbitrage pour
-- un staging ; il ne tient pas au mart, dont le contrat `not_null` sur
-- `loaded_at` vaut pour toutes les branches et n'est pas negociable pour une
-- seule d'entre elles.
--
-- LA VALEUR EXISTE, ELLE N'ETAIT PAS MIRROITEE. Chaque execution d'un import
-- fichier porte son heure dans `app.datastream_executions.created_at`, et le
-- ledger la porte aussi. Rien a fabriquer, rien a inferer : une jointure sur
-- `execution_id` rend la seule reponse vraie a << quand cette ligne a-t-elle
-- ete chargee ? >>. Le staging la lit, le mart la relaie, et personne n'invente.
--
-- POURQUOI L'EXECUTION ET NON LE LEDGER. Les lignes de landing portent
-- `execution_id`, pas `ledger_id` -- joindre le ledger demanderait un detour par
-- une colonne que la donnee ne porte pas. Et une execution existe pour les DEUX
-- chemins fichier (import et Sheets), la ou le ledger ne couvre que l'import.

CREATE OR REPLACE VIEW app.managed_feed_execution_v AS
SELECT
    e.project_id,
    d.org_id,
    e.datastream_id,
    e.id          AS execution_id,
    e.created_at  AS loaded_at
FROM app.datastream_executions e
JOIN app.datastreams d
  ON d.id = e.datastream_id AND d.project_id = e.project_id
WHERE d.source_kind = 'managed_feed';

COMMENT ON VIEW app.managed_feed_execution_v IS
    'Par execution d un flux fichier : QUAND elle a charge. La colonne que le '
    'chemin fichier n apposait pas sur ses lignes et que le mart exige non nulle '
    '-- elle a toujours existe dans app.datastream_executions, elle n etait pas '
    'mirroitee (story 69.2). Le staging la joint sur execution_id ; rien n est '
    'fabrique.';

COMMENT ON VIEW app.managed_feed_grain_v IS
    'Par Datastream managed_feed : sa table de landing, la MAILLE de son mapping '
    'publie (migration 227) et, depuis la 299, ce que chaque colonne EST -- le '
    'jour, les axes, les mesures additives, et les mesures non additives que le '
    'mart doit refuser en les nommant. Les noms sont des cibles canoniques, '
    'parce que c est ce qui atterrit physiquement. `has_grain` a FALSE veut dire '
    '<< pas de cle, dedoublonnage impossible >> ; `date_column` a NULL veut dire '
    '<< ce flux ne decrit pas des faits journaliers >>. Les deux se disent, '
    'jamais se taisent.';

COMMIT;
