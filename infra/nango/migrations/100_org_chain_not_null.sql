-- 100_org_chain_not_null.sql
--
-- Poser la contrainte que l'architecture specifiait depuis le debut et que
-- personne n'a jamais appliquee.
--
-- La chaine du produit est : PREMIER LOGIN -> creer ou rejoindre une
-- organisation (obligatoire) ; TOUT projet appartient a une organisation, sans
-- exception ; TOUT flux appartient a un projet ; TOUT credential se rattache au
-- niveau de l'organisation.
--
-- architecture-org-tenancy-2026-07-20.md le dit noir sur blanc :
--   §3.4 projects        : « + org_id NOT NULL FK -> organizations »
--   §3.8 flux/datastreams: « org_id TEXT NOT NULL FK -> organizations --
--                            remplace project_id, l'isolation data se joue ici »
--   §3.5 credential      : « owner_org_id FK -> organizations, remplace project_id »
--   §4.1                 : « Org = frontiere. Toute table client-facing porte org_id. »
--
-- Ce qui s'est passe : la story 21.1 a cree `projects.org_id` NULLABLE « le
-- temps du backfill puis renseignee », et 21.3/21.4 ont fait de meme pour
-- `connection_ref.owner_org_id` et `datastreams.org_id` en gardant `project_id`
-- « en compat ». Le resserrage etait renvoye a « une story ulterieure dediee »
-- qui n'a jamais ete planifiee : epic-21 s'arrete a 21.6/21.7. Les colonnes
-- sont donc restees nullables, et un projet orphelin -- sans entrepot ou
-- atterrir, hors de tout cloisonnement -- est reste techniquement possible.
-- C'est cette porte que la migration ferme.
--
-- Prealable verifie avant application : aucune ligne violante ne subsiste
-- (`datastreams` et `connection_ref` vides, le seul projet orphelin supprime).
-- La migration ECHOUE bruyamment si ce n'est plus vrai -- ce qui est le
-- comportement voulu : il faudrait alors backfiller, pas relacher la contrainte.

BEGIN;

-- Garde-fou explicite : nommer ce qui bloque plutot que laisser Postgres
-- renvoyer un « column contains null values » sans dire lequel.
DO $guard$
DECLARE
    n_projects bigint;
    n_flux bigint;
    n_cred bigint;
BEGIN
    SELECT count(*) INTO n_projects FROM app.projects WHERE org_id IS NULL;
    SELECT count(*) INTO n_flux FROM app.datastreams WHERE org_id IS NULL;
    SELECT count(*) INTO n_cred FROM app.connection_ref WHERE owner_org_id IS NULL;
    IF n_projects > 0 OR n_flux > 0 OR n_cred > 0 THEN
        RAISE EXCEPTION
            'org chain: % projet(s), % flux et % credential(s) sans organisation -- '
            'backfiller AVANT de poser la contrainte',
            n_projects, n_flux, n_cred;
    END IF;
END
$guard$;

ALTER TABLE app.projects       ALTER COLUMN org_id       SET NOT NULL;
ALTER TABLE app.datastreams    ALTER COLUMN org_id       SET NOT NULL;
ALTER TABLE app.connection_ref ALTER COLUMN owner_org_id SET NOT NULL;

COMMENT ON COLUMN app.projects.org_id IS
    'Organisation proprietaire. NOT NULL (mig 100) : un projet sans org n''a pas '
    'd''entrepot ou atterrir et echappe au cloisonnement -- ce n''est pas un projet '
    'degrade, c''est un projet impossible.';

COMMENT ON COLUMN app.datastreams.org_id IS
    'Organisation proprietaire du flux. NOT NULL (mig 100) : l''isolation data se '
    'joue ici (architecture-org-tenancy §3.8).';

COMMENT ON COLUMN app.connection_ref.owner_org_id IS
    'Organisation proprietaire du credential. NOT NULL (mig 100) : un credential se '
    'rattache au niveau ORGANISATION, pas au projet (architecture-org-tenancy §3.5).';

COMMIT;
