-- 227 -- Un managed feed declare la cle qui le supersede.
--
-- POURQUOI. Renvoyer un fichier qui recouvre des jours deja recus empile les
-- lignes : `land_raw_rows` est un INSERT, et le MERGE de promotion a pour cle
-- l'`execution_id` (il empeche de promouvoir deux fois le meme run, jamais de
-- reinserer les memes jours). Les connecteurs n'ont pas ce probleme -- 52
-- modeles de staging sur 53 portent
--
--     QUALIFY ROW_NUMBER() OVER (PARTITION BY <maille> ORDER BY pull_id DESC) = 1
--
-- et le brut reste ajout-seul pendant que la LECTURE ne garde que le dernier
-- chargement. Le chemin fichier n'a aucun modele, donc rien ne supersede.
--
-- CE QUE CETTE VUE APPORTE, et rien de plus : de quoi ecrire ce modele. La
-- table de landing porte un nom deterministe (`managed_feed_<datastream_id>`,
-- `managed_feed_ledger.allocate_landing_relation`), les lignes portent deja leur
-- `execution_id` (`csv_excel_import`, colonnes de provenance) et cet identifiant
-- est un `dse_<ULID>` monotone -- donc `ORDER BY execution_id DESC` est bien
-- « le dernier chargement d'abord ». Il manquait UNE chose cote entrepot : la
-- MAILLE, qui vit dans le mapping publie et que rien ne mirroitait.
--
-- LA MAILLE VIENT DE LA VERSION COURANTE, JAMAIS DE LA PLUS RECENTE. Un mapping
-- appose mais non publie ne doit pas changer la cle qui dedoublonne ce qui est
-- deja lu ; sinon la lecture change sous les pieds de l'operateur avant qu'il
-- ait confirme quoi que ce soit.
--
-- UN FLUX SANS MAILLE EST RENDU AVEC UNE MAILLE VIDE, PAS OMIS. Le modele qui
-- lit cette vue doit pouvoir DIRE « ce flux n'a pas de cle, je ne peux pas le
-- dedoublonner » plutot que de l'inclure en silence avec ses doublons. Une
-- absence omise se lit comme une absence de probleme.

BEGIN;

CREATE OR REPLACE VIEW app.managed_feed_grain_v AS
SELECT
    d.project_id,
    d.org_id,
    d.id                                              AS datastream_id,
    'managed_feed_' || replace(d.id, '-', '_')        AS landing_table,
    d.current_mapping_version_id                      AS mapping_version_id,
    COALESCE(v.mapping_payload -> 'grain', '[]'::jsonb) AS grain_columns,
    jsonb_array_length(COALESCE(v.mapping_payload -> 'grain', '[]'::jsonb)) > 0
                                                      AS has_grain
FROM app.datastreams d
LEFT JOIN app.datastream_mapping_versions v
       ON v.id = d.current_mapping_version_id
      AND v.project_id = d.project_id
WHERE d.source_kind = 'managed_feed';

COMMENT ON VIEW app.managed_feed_grain_v IS
    'Par Datastream managed_feed : sa table de landing et la MAILLE de son '
    'mapping publie. Sert au modele de staging qui supersede un fichier renvoye '
    '-- PARTITION BY cette maille, ORDER BY execution_id DESC. `has_grain` a '
    'FALSE veut dire << pas de cle, dedoublonnage impossible >>, ce que le '
    'lecteur doit dire plutot que taire.';

COMMIT;
