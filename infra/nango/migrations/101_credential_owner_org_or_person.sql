-- 101_credential_owner_person_org_scope.sql
--
-- Un credential a DEUX rattachements, et ils ne disent pas la meme chose :
--
--   * owner_identity -- LA PERSONNE qui l'a branche. C'est son acces, sous son
--     nom : c'est elle qui a consenti chez le fournisseur, et c'est a elle que
--     l'audit renvoie.
--   * owner_org_id   -- L'ORGANISATION dans laquelle il est utilisable. C'est ce
--     qui permet a un AUTRE membre de lancer la sync : un collegue rafraichit un
--     report qui s'en nourrit sans posseder lui-meme l'acces a la source.
--
-- Les deux sont obligatoires, et ce n'est pas une redondance : sans la personne,
-- un credential n'a personne pour en repondre ; sans l'organisation, il n'est
-- utilisable que par son proprietaire et la sync se bloque des qu'il n'est pas la.
--
-- Corrige la migration 100 : elle a rendu `owner_org_id` NOT NULL, ce qui etait
-- juste, mais la table ne portait AUCUNE colonne de personne -- le proprietaire
-- reel n'etait donc nulle part, et  l'acces de qui ?  restait sans reponse.
--
-- Consequence applicative a tenir (elle ne se joue pas en SQL) : le declenchement
-- d'une sync doit s'autoriser sur l'appartenance a `owner_org_id`, JAMAIS sur
-- l'egalite entre l'appelant et `owner_identity`. Un controle base sur le
-- proprietaire redonnerait exactement le blocage que cette colonne existe pour
-- eviter.
--
-- La table est vide a l'application : aucun backfill n'est requis.

BEGIN;

-- Pas de FK vers user_profiles, DELIBEREMENT : cette table ne se remplit que
-- lorsqu'un utilisateur renseigne son profil, alors que l'identite existe des le
-- premier jeton. Une FK ferait echouer la creation de credential pour quiconque
-- n'a pas encore de profil -- exactement le piege qui rendait le projet orphelin.
-- Meme choix que app.org_members.identity et app.projects.created_by : la chaine
-- d'identite est le sujet du jeton (AD-14), pas une ligne de table.
ALTER TABLE app.connection_ref
    ADD COLUMN IF NOT EXISTS owner_identity TEXT;

DO $backfill$
DECLARE
    n bigint;
BEGIN
    SELECT count(*) INTO n FROM app.connection_ref WHERE owner_identity IS NULL;
    IF n > 0 THEN
        RAISE EXCEPTION
            'credential: % ligne(s) sans owner_identity -- backfiller la personne '
            'proprietaire AVANT de poser la contrainte', n;
    END IF;
END
$backfill$;

ALTER TABLE app.connection_ref ALTER COLUMN owner_identity SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_connection_ref_owner_identity
    ON app.connection_ref (owner_identity);

COMMENT ON COLUMN app.connection_ref.owner_org_id IS
    'Organisation dans laquelle le credential est UTILISABLE : c''est elle qui '
    'autorise un autre membre a lancer la sync. NOT NULL (mig 100).';

COMMENT ON COLUMN app.connection_ref.owner_identity IS
    'Personne qui a branche le credential -- son acces, sous son nom. NOT NULL '
    '(mig 101). N''est PAS un controle d''usage : l''autorisation de lancer une '
    'sync se resout sur owner_org_id, sinon la sync se bloque des que le '
    'proprietaire n''est pas la.';

COMMIT;
