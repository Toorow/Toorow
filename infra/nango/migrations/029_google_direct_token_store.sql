-- infra/nango/migrations/029_google_direct_token_store.sql
--
-- Story 18.1: Store de tokens Google chiffre dans app.connection_ref.
--
-- CONTEXTE / DEROGATION A AD-3 (AD-21, RATIFIED 2026-07-18 -- directive Jean).
-- La migration 001_create_connection_ref.sql declare explicitement en tete :
--   "AD-3 invariant: NO token columns. Tokens live only in Nango."
-- Cette migration 029 est la PREMIERE derogation materielle a cet invariant.
-- Elle est STRICTEMENT bornee au stack Google (GA4, GSC, Google Ads, Sheets) et
-- ne leve l'interdit d'AD-3 que sous les trois garanties d'AD-21 qui preservent
-- l'intention d'origine (eviter deux proprietaires de tokens et des modules qui
-- inventent leur propre stockage) :
--
--   1. Chiffrement au repos equivalent a Nango : le blob (encrypted_token_blob)
--      est chiffre par server/core/google_token_store.py avec une cle derivee de
--      tenant_keys (Secret Manager en Phase B) -- la MEME classe de cle que Nango.
--      Postgres ne voit QUE du ciphertext opaque ; JAMAIS de colonne token en clair
--      (NFR3). Il n'existe deliberement AUCUNE colonne access_token / refresh_token.
--   2. Un seul writer : google_token_store.py est l'unique composant qui ecrit,
--      lit et dechiffre ce blob (AD-8 single ownership). Aucun module connecteur
--      n'y touche ; tous passent par la facade get_fresh_token (18.3).
--   3. Audit : chaque emission / refresh / revocation est tracee (AD-14
--      On-Behalf-Of), comme les connexions Nango le sont.
--
-- Ce qui NE change PAS : pour tout provider non-Google, auth_path reste 'nango',
-- encrypted_token_blob reste NULL -- AD-3 demeure vrai pour ces lignes (aucune
-- colonne token n'est peuplee). Nango reste le chemin par defaut.
--
-- Colonnes ajoutees (ADDITIVES, idempotentes, toutes NULL-ables sauf auth_path
-- qui porte un DEFAULT non destructif 'nango') :
--   auth_path            : TEXT NOT NULL DEFAULT 'nango', CHECK IN ('nango',
--                          'google_direct'). Route de resolution du token (18.3).
--                          Les lignes existantes recoivent 'nango' automatiquement.
--   encrypted_token_blob : BYTEA NULL. Blob chiffre (access + refresh + metadata
--                          + version de format), produit par google_token_store.
--                          NULL pour toute connexion Nango. Il n'y a PAS de colonne
--                          token en clair -- c'est intentionnel (NFR3).
--   token_expiry         : TIMESTAMPTZ NULL. Expiration de l'access token, lue en
--                          clair pour la sante locale (18.3) -- ce n'est pas un secret.
--   granted_scopes       : TEXT[] NULL. Scopes Google accordes (analytics.readonly,
--                          webmasters.readonly, adwords, spreadsheets.readonly...).
--
-- Invariants :
--   AD-21 : derogation encadree a AD-3, bornee au stack Google (voir en-tete).
--   NFR3  : aucun token en clair -- seul un blob chiffre BYTEA est persiste.
--   AI-44 : fichier nomme 029_google_direct_token_store.sql (prefixe zero-padde 3 chiffres).
--
-- Migration idempotente : ADD COLUMN IF NOT EXISTS + DROP CONSTRAINT IF EXISTS
-- avant ADD CONSTRAINT.
--
-- Appliquer avec :
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/029_google_direct_token_store.sql
--

BEGIN;

-- 1. auth_path : route de resolution du token. DEFAULT 'nango' => les lignes
--    Nango existantes restent inchangees (non-regression AD-3).
ALTER TABLE app.connection_ref
    ADD COLUMN IF NOT EXISTS auth_path TEXT NOT NULL DEFAULT 'nango';

ALTER TABLE app.connection_ref
    DROP CONSTRAINT IF EXISTS chk_connection_ref_auth_path;
ALTER TABLE app.connection_ref
    ADD CONSTRAINT chk_connection_ref_auth_path
    CHECK (auth_path IN ('nango', 'google_direct'));

-- 2. encrypted_token_blob : blob chiffre (access + refresh + metadata). BYTEA
--    NULL. JAMAIS de colonne token en clair -- Postgres ne voit que du ciphertext.
ALTER TABLE app.connection_ref
    ADD COLUMN IF NOT EXISTS encrypted_token_blob BYTEA;

-- 3. token_expiry : expiry de l'access token, en clair (sante locale, pas un secret).
ALTER TABLE app.connection_ref
    ADD COLUMN IF NOT EXISTS token_expiry TIMESTAMPTZ;

-- 4. granted_scopes : scopes Google accordes (pas un secret).
ALTER TABLE app.connection_ref
    ADD COLUMN IF NOT EXISTS granted_scopes TEXT[];

-- Index partiel : recherche rapide des connexions google_direct (18.3 / 18.4).
CREATE INDEX IF NOT EXISTS idx_connection_ref_auth_path
    ON app.connection_ref (auth_path)
    WHERE auth_path = 'google_direct';

COMMIT;
