-- infra/nango/migrations/028_verification_source_prefs.sql
--
-- Story 17.1: Ajout des 3 colonnes de source de vérification à app.project_preferences.
--
-- Colonnes ajoutées (toutes NULL-ables, additive, aucun défaut destructif) :
--   verification_source_type : TEXT NULL, CHECK IN ('ga4','shopify','stripe') OR NULL.
--                              Optionnel — la déduplication est opt-in par projet.
--   verification_source_id   : TEXT NULL — identifiant du datastream/flux de données
--                              désigné comme source de vérité (FK applicative, pas SQL,
--                              car un datastream peut être supprimé sans invalider la
--                              préférence ; la validation croisée est faite dans l'API).
--   lead_event_name          : TEXT NULL — nom de l'événement GA4 qui représente un lead
--                              (ex : 'generate_lead', 'form_submit'). Pertinent uniquement
--                              quand verification_source_type = 'ga4'. Le défaut applicatif
--                              est 'generate_lead' ; aucun DEFAULT SQL pour rester honest sur
--                              l'absence de valeur configurée.
--
-- Invariants :
--   AD-5  : la validation cross-projet de verification_source_id est faite dans l'API.
--   AD-8  : Postgres est le seul writer ; DuckDB/BigQuery accède via le miroir gouverné
--           (mirror_sync.py, Story 4.4). Ces colonnes sont propagées automatiquement par
--           le SELECT * FROM app.project_preferences déjà en place.
--   AI-44 : fichier nommé 028_verification_source_prefs.sql (préfixe zéro-paddé 3 chiffres).
--
-- Migration idempotente : ADD COLUMN IF NOT EXISTS + DROP CONSTRAINT IF EXISTS avant ADD.
--
-- Appliquer avec :
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/028_verification_source_prefs.sql
--

BEGIN;

-- 1. Colonne verification_source_type : valeurs acceptées ou NULL.
ALTER TABLE app.project_preferences
    ADD COLUMN IF NOT EXISTS verification_source_type TEXT;

-- Contrainte CHECK idempotente (DROP IF EXISTS avant ADD).
ALTER TABLE app.project_preferences
    DROP CONSTRAINT IF EXISTS chk_project_preferences_vstype;
ALTER TABLE app.project_preferences
    ADD CONSTRAINT chk_project_preferences_vstype
    CHECK (
        verification_source_type IS NULL
        OR verification_source_type IN ('ga4', 'shopify', 'stripe')
    );

-- 2. Colonne verification_source_id : identifiant libre du datastream/profil source.
ALTER TABLE app.project_preferences
    ADD COLUMN IF NOT EXISTS verification_source_id TEXT;

-- 3. Colonne lead_event_name : nom de l'événement lead GA4.
ALTER TABLE app.project_preferences
    ADD COLUMN IF NOT EXISTS lead_event_name TEXT;

-- Index utile pour les recherches par type de source (17.2 lira dim_project filtré).
CREATE INDEX IF NOT EXISTS idx_project_preferences_vstype
    ON app.project_preferences (verification_source_type)
    WHERE verification_source_type IS NOT NULL;

COMMIT;
