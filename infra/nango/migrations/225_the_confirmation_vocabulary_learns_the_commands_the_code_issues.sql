-- 225 -- La liste des commandes confirmables apprend celles que le code emet.
--
-- POURQUOI. `entry_confirmations_command_type_check` a ete pose par la
-- migration 116 puis reecrit par la 131, avec quatre valeurs. Depuis, le code a
-- ajoute TROIS commandes et personne n'a rouvert la contrainte :
--
--     datastream.setup.create_draft            (core/entry_confirmations.py:26)
--     datastream.candidate.publish_activate    (:27)
--     governance.country.publish               (:28)
--
-- Chacune levait `CheckViolation` a l'emission, sous un 503 fourre-tout qui ne
-- nommait rien. Mesure vivante 2026-08-07 : `POST .../draft-confirmations` ->
-- 503 `preconfiguration_unavailable`, et dans le journal
-- « new row for relation "entry_confirmations" violates check constraint ».
-- Aucune confirmation de creation de Datastream n'a donc JAMAIS pu etre emise,
-- dans aucun des trois modes -- la derniere porte du parcours assistant.
--
-- MEME FAMILLE QUE LA 152 ET SON `project_change_set` : une constante ajoutee au
-- point d'ecriture, une contrainte laissee en arriere, et un refus qui se lit
-- comme un bug applicatif. La liste vit maintenant aux DEUX endroits, et ce
-- commentaire dit lequel fait foi : `core.entry_confirmations`. Une commande
-- ajoutee la-bas se pose ici dans le meme commit.
--
-- ADDITIVE : la contrainte s'elargit, aucune ligne existante ne change.

BEGIN;

ALTER TABLE app.entry_confirmations
    DROP CONSTRAINT IF EXISTS entry_confirmations_command_type_check;

ALTER TABLE app.entry_confirmations
    ADD CONSTRAINT entry_confirmations_command_type_check
    CHECK (command_type IN (
        'hosted.entry_scope.create',
        'instance.claim',
        'project.settings.activate',
        'project.access.change',
        'datastream.setup.create_draft',
        'datastream.candidate.publish_activate',
        'governance.country.publish'
    ));

COMMENT ON COLUMN app.entry_confirmations.command_type IS
    'L''acte que cette confirmation autorise. La liste fait foi dans '
    '`core.entry_confirmations` (constantes *_COMMAND) ; cette CHECK la '
    'recopie. Ajouter une commande la-bas sans la poser ici la fait echouer a '
    'l''emission, sous un 503 qui ne la nomme pas.';

COMMIT;
