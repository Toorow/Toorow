-- 231 -- Une configuration differee se complete par un SECOND acte.
--
-- POURQUOI. Un flux entrant se materialise AVANT sa premiere livraison : c'est la
-- seule facon d'avoir une adresse ou recevoir (migration 224). Son plan et son
-- mapping viennent plus tard, quand le fichier a revele sa forme -- et ce
-- second passage est un ACTE GOUVERNE A PART ENTIERE : sa propre revue finale,
-- sa propre confirmation, sa propre operation.
--
-- Trois unicites l'interdisaient, chacune supposant qu'un brouillon ne se
-- materialise qu'une fois :
--     (project_id, draft_id)        -- un brouillon, une materialisation
--     (project_id, datastream_id)   -- un Datastream, une materialisation
--     (project_id, candidate_execution_id)
--
-- LA LIGNE NE SE REECRIT PAS, ET C'EST BIEN. `trg_..._immutable` (migration 137)
-- refuse tout UPDATE sur cette table, et cette immuabilite est la propriete meme
-- de la preuve : on n'efface pas ce qui a ete atteste. La completion n'est donc
-- pas une correction de la premiere ligne -- c'est une seconde ligne, qui dit
-- autre chose : << voici les versions, issues de CETTE revue-la >>.
--
-- CE QUI REMPLACE LES DEUX PREMIERES : l'unicite par REVUE FINALE. Une revue
-- produit au plus une materialisation ; deux revues distinctes produisent deux
-- lignes. Rejouer la meme revue reste refuse -- l'idempotence est preservee la
-- ou elle compte -- et la lignee d'un brouillon devient lisible dans l'ordre.
--
-- La troisieme est GARDEE telle quelle : deux materialisations ne peuvent pas
-- revendiquer la meme execution candidate, et rien dans la completion ne le
-- demande (une completion mint la sienne).
--
-- Additive au sens qui compte : aucune ligne existante n'est touchee, et toute
-- paire (brouillon, revue) deja ecrite reste unique.

BEGIN;

ALTER TABLE app.datastream_setup_materializations
    DROP CONSTRAINT IF EXISTS datastream_setup_materializations_project_id_draft_id_key;

ALTER TABLE app.datastream_setup_materializations
    DROP CONSTRAINT IF EXISTS datastream_setup_materializations_project_id_datastream_id_key;

ALTER TABLE app.datastream_setup_materializations
    DROP CONSTRAINT IF EXISTS uq_dsm_project_draft_final_review;
ALTER TABLE app.datastream_setup_materializations
    ADD CONSTRAINT uq_dsm_project_draft_final_review
    UNIQUE (project_id, draft_id, final_review_id);

COMMENT ON TABLE app.datastream_setup_materializations IS
    'Une ligne par ACTE de materialisation, jamais une par brouillon. Un flux '
    'entrant en produit deux : la creation -- qui donne l''adresse et laisse les '
    'trois colonnes de version a NULL (migration 224) -- puis la completion, '
    'quand la premiere livraison a revele son schema. La table est immuable '
    '(migration 137) : une completion AJOUTE, elle ne corrige pas.';

COMMIT;
