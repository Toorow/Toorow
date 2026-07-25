-- infra/nango/migrations/044_mediaplan_alert_prefs.sql
--
-- Story 22.6 : seuils d'alerte pacing médiaplan, configurables par projet (FR9).
--
-- Décision 6 du spike 22-0 : jamais de seuil en dur ; défaut ±10 % asymétrique
-- (> +10 % rouge dépassement, < −10 % jaune sous-livraison).
--
-- Deux colonnes NULLABLE sur app.project_preferences (pattern 033) :
--   mediaplan_overrun_threshold     DOUBLE PRECISION NULL
--       NULL => défaut documenté DEFAULT_OVERRUN_THRESHOLD = 0.10 (10 %)
--       Valeur positive : fraction décimale (0.20 = 20 %).
--   mediaplan_underdelivery_threshold DOUBLE PRECISION NULL
--       NULL => défaut documenté DEFAULT_UNDERDELIVERY_THRESHOLD = -0.10 (-10 %)
--       Valeur négative ou nulle attendue (ex. -0.20).
--
-- Invariants :
--   AD-8  : Postgres seul writer ; dbt lit via le miroir existant.
--   AD-9  : le seuil nul ou incohérent est rejeté par les CHECK ; NULL = défaut
--           documenté, jamais silencieux (le code cite threshold_source).
--   Additif et idempotent : ADD COLUMN IF NOT EXISTS, DROP/ADD CHECK idempotents.
--
-- Numérotation : 044 est le suivant libre après 043_gsc_full_coverage.sql.
-- Vérifier que 043 est toujours le plus élevé au moment du merge.
--
-- Application :
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector \
--       -f /migrations/044_mediaplan_alert_prefs.sql

BEGIN;

ALTER TABLE app.project_preferences
    ADD COLUMN IF NOT EXISTS mediaplan_overrun_threshold DOUBLE PRECISION;

ALTER TABLE app.project_preferences
    ADD COLUMN IF NOT EXISTS mediaplan_underdelivery_threshold DOUBLE PRECISION;

-- Contrainte : le seuil de dépassement doit être positif si fourni.
-- (un seuil nul ou négatif rendrait l'alerte rouge inaccessible ou invertie)
ALTER TABLE app.project_preferences
    DROP CONSTRAINT IF EXISTS chk_project_preferences_overrun_threshold;
ALTER TABLE app.project_preferences
    ADD CONSTRAINT chk_project_preferences_overrun_threshold
    CHECK (
        mediaplan_overrun_threshold IS NULL
        OR mediaplan_overrun_threshold > 0
    );

-- Contrainte : le seuil de sous-livraison doit être négatif si fourni.
-- (une valeur positive ou nulle serait conceptuellement incohérente)
ALTER TABLE app.project_preferences
    DROP CONSTRAINT IF EXISTS chk_project_preferences_underdelivery_threshold;
ALTER TABLE app.project_preferences
    ADD CONSTRAINT chk_project_preferences_underdelivery_threshold
    CHECK (
        mediaplan_underdelivery_threshold IS NULL
        OR mediaplan_underdelivery_threshold < 0
    );

-- Index de dédup pour les firings de type 'mediaplan_pace' :
-- une seule alerte par (project_id, metric, window_date) par nuit, identique
-- au pattern alert_firings_dedup_anomaly (migration 013).
-- Le metric encode le niveau et la règle, ex. 'mediaplan_pace_overrun:plan-A:line-digital'.
-- NOTE : on réutilise la table alert_firings existante avec type='mediaplan_pace' ;
-- le dedup se fait exactement comme pour 'anomaly' (project_id, metric, window_date).
CREATE UNIQUE INDEX IF NOT EXISTS alert_firings_dedup_mediaplan_pace
    ON app.alert_firings (project_id, metric, window_date)
    WHERE type = 'mediaplan_pace';

COMMIT;
