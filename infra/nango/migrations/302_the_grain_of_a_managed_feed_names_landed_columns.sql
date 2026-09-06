-- 302 -- La maille d'un managed feed nomme des colonnes qui EXISTENT.
--
-- LA REGLE ETAIT DEJA ECRITE, ET UNE COLONNE NE L'AVAIT PAS SUIVIE. L'en-tete de
-- la migration 299 la pose en toutes lettres :
--
--   << LES NOMS SONT DES CIBLES CANONIQUES, PAS DES `field_id`. Ce qui atterrit
--   physiquement, c'est la projection epinglee -- `_apply_governed_mapping` ne
--   pose que les `canonical_target`. Une vue qui rendrait les `field_id`
--   nommerait des colonnes que la table de landing n'a pas des que le mapping
--   renomme quoi que ce soit. >>
--
-- `date_column`, `dimension_columns`, `measure_columns` et `non_additive_columns`
-- obeissent a cette regle. `grain_columns`, heritee de la 227, ne l'a jamais
-- suivie : elle relaie `mapping_payload -> 'grain'` tel quel, donc des `field_id`.
-- La migration a ecrit la regle et ne l'a pas appliquee a la colonne qu'elle
-- reprenait.
--
-- CE QUE CA CASSE, MESURE LE 2026-08-23. `managed_feed_superseding` et
-- `stg_managed_feed_facts` composent tous deux leur cle de maille en concatenant
-- les noms de `grain_columns` CONTRE LA RELATION ATTERRIE. Un fichier dont la
-- maille est `["day", "video_id"]` et dont le mapping renomme `day` -> `date` --
-- le cas ORDINAIRE d'une colonne de date, pas un cas de bord -- fait echouer le
-- build entier :
--
--   Binder Error: Referenced column "day" not found in FROM clause!
--   Candidate bindings: "date"
--
-- Personne ne l'avait vu parce que chaque fixture existante emploie une maille
-- dont la cible canonique EGALE le `field_id` source : le renommage, qui est la
-- raison d'etre d'un mapping, n'etait jamais exerce sur une colonne de maille.
-- Trouve en construisant le lot de semences de l'epine d'entites (AI-303), sur
-- une capture de la vraie chaine des epics 68-69.
--
-- CE QUE CETTE VUE REND MAINTENANT. `grain_columns` porte les noms ATTERRIS, dans
-- l'ordre de la maille -- l'ordre compte : la cle est une concatenation, et deux
-- ordres donnent deux cles pour la meme ligne. Le nom source reste lisible sous
-- `grain_field_ids`, parce que c'est lui qui joint la maille aux champs du
-- mapping et qu'un lecteur qui perd cette jonction ne peut plus expliquer d'ou
-- vient une colonne. Cette colonne est AJOUTEE EN FIN de vue, la ou elle se lit
-- le moins bien, parce que `CREATE OR REPLACE VIEW` ne sait qu'ajouter a la fin
-- et que l'alternative -- un DROP -- emporterait les droits et les dependants.
--
-- UNE COLONNE DE MAILLE QUI N'ATTERRIT PAS EST OMISE, ET `has_grain` LE DIT. Un
-- champ de maille `excluded` ou non confirme ne pose aucune colonne ; le garder
-- dans la liste reconstruirait exactement le defaut repare ici. `has_grain`
-- devient donc vrai quand il reste au moins une colonne REELLE -- une maille
-- entierement non atterrie n'est pas une maille, et un flux qui la declarait
-- passait auparavant pour dedoublonnable.
--
-- Cette migration ne cree ni table ni colonne : elle REMPLACE une vue. Rien a
-- retro-remplir, rien a verrouiller.
--
-- Contrat : docs/product-architecture/data.md (managed feed), stories 69.1/69.2.

BEGIN;

CREATE OR REPLACE VIEW app.managed_feed_grain_v AS
WITH published AS (
    SELECT
        d.project_id,
        d.org_id,
        d.id                                       AS datastream_id,
        'managed_feed_' || replace(d.id, '-', '_') AS landing_table,
        d.current_mapping_version_id               AS mapping_version_id,
        COALESCE(v.mapping_payload -> 'grain', '[]'::jsonb) AS grain_field_ids,
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
),
-- LA MAILLE, TRADUITE, DANS SON ORDRE. `WITH ORDINALITY` porte la position que
-- le mapping a declaree ; trier sur elle est ce qui fait que la cle composee est
-- la meme d'un build a l'autre.
grain_landed AS (
    SELECT
        p.datastream_id,
        COALESCE(
            jsonb_agg(l.column_name ORDER BY g.ordinality)
            FILTER (WHERE l.column_name IS NOT NULL),
            '[]'::jsonb
        ) AS grain_columns
    FROM published p
    CROSS JOIN LATERAL jsonb_array_elements_text(p.grain_field_ids)
        WITH ORDINALITY AS g(field_id, ordinality)
    LEFT JOIN landed l
           ON l.datastream_id = p.datastream_id
          AND l.field_id = g.field_id
    GROUP BY p.datastream_id
)
SELECT
    p.project_id,
    p.org_id,
    p.datastream_id,
    p.landing_table,
    p.mapping_version_id,
    COALESCE(gl.grain_columns, '[]'::jsonb)      AS grain_columns,
    jsonb_array_length(COALESCE(gl.grain_columns, '[]'::jsonb)) > 0 AS has_grain,
    -- The day this feed's rows are about. NULL is an answer: a file whose grain
    -- names no date does not describe daily facts.
    (
        SELECT l.column_name FROM landed l
        WHERE l.datastream_id = p.datastream_id
          AND l.semantic_role = 'primary_date'
          AND p.grain_field_ids ? l.field_id
        ORDER BY l.column_name
        LIMIT 1
    ) AS date_column,
    -- The axes: every grain column that is not the day.
    COALESCE((
        SELECT jsonb_agg(l.column_name ORDER BY l.column_name) FROM landed l
        WHERE l.datastream_id = p.datastream_id
          AND l.semantic_role = 'dimension'
          AND p.grain_field_ids ? l.field_id
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
    ), '[]'::jsonb) AS non_additive_columns,
    -- APPENDED LAST, and the position is not a style choice: `CREATE OR REPLACE
    -- VIEW` may only ADD columns at the end. Inserting `grain_field_ids` beside
    -- `grain_columns`, where it reads best, is refused by Postgres -- "ne peut
    -- pas modifier le nom de la colonne has_grain" -- and the alternative is a
    -- DROP, which would take every grant and every dependent with it.
    p.grain_field_ids                            AS grain_field_ids
FROM published p
LEFT JOIN grain_landed gl ON gl.datastream_id = p.datastream_id;

COMMIT;
