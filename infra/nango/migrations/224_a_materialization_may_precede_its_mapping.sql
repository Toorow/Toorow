-- 224 -- Une materialisation peut preceder son mapping.
--
-- POURQUOI. `app.datastream_setup_materializations` exigeait un
-- `plan_version_id`, un `mapping_version_id` et un `candidate_execution_id`.
-- Cela suppose qu'un Datastream ne peut etre cree qu'une fois son schema connu.
-- C'est vrai d'un connecteur et d'un fichier depose pendant la configuration.
-- C'est FAUX d'un canal entrant : son fichier n'existe pas avant qu'une
-- livraison arrive, une adresse ne s'emet que contre un Datastream materialise
-- (`docs/product-architecture/datastream-workbench-and-wizard.md`, Incomplete
-- if), et `inbound_credentials._require_receivable` autorise deja explicitement
-- l'etat `draft` -- « Allow draft discovery and active intake ». L'ordre etait
-- donc prevu partout SAUF dans ces trois colonnes.
--
-- CE QUE NULL VEUT DIRE ICI, et rien d'autre : ces versions n'existent PAS
-- ENCORE. Ce n'est pas un zero, ce n'est pas un defaut, et ce n'est pas une
-- materialisation incomplete -- le Datastream existe, il porte son canal, il
-- peut recevoir. Le plan et le mapping naitront de la premiere livraison, qui
-- est l'evidence que ce canal ne pouvait pas porter avant.
--
-- CE QUI RESTE INTERDIT. Les trois colonnes restent liees a leur cible par
-- leurs cles etrangeres : une version nommee ici doit exister. Seule l'ABSENCE
-- devient dicible.
--
-- Additive et reversible sans perte : aucune ligne existante n'est touchee,
-- toutes portent leurs trois versions et continuent de les porter.

BEGIN;

ALTER TABLE app.datastream_setup_materializations
    ALTER COLUMN plan_version_id DROP NOT NULL;

ALTER TABLE app.datastream_setup_materializations
    ALTER COLUMN mapping_version_id DROP NOT NULL;

ALTER TABLE app.datastream_setup_materializations
    ALTER COLUMN candidate_execution_id DROP NOT NULL;

-- Les trois vont ensemble ou aucune : un plan sans son mapping, ou un candidat
-- sans le plan qu'il execute, serait un etat que rien ne sait lire. La garde
-- dit cette regle plutot que de la laisser a la discipline des appelants.
ALTER TABLE app.datastream_setup_materializations
    DROP CONSTRAINT IF EXISTS chk_dsm_versions_travel_together;
ALTER TABLE app.datastream_setup_materializations
    ADD CONSTRAINT chk_dsm_versions_travel_together
    CHECK (
        (plan_version_id IS NULL
         AND mapping_version_id IS NULL
         AND candidate_execution_id IS NULL)
        OR
        (plan_version_id IS NOT NULL
         AND mapping_version_id IS NOT NULL
         AND candidate_execution_id IS NOT NULL)
    );

COMMENT ON COLUMN app.datastream_setup_materializations.plan_version_id IS
    'La version de plan figee a la materialisation, ou NULL quand le canal ne '
    'pouvait pas encore porter son schema (canal entrant avant sa premiere '
    'livraison). NULL = pas encore, jamais = incomplet.';

COMMIT;
