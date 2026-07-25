-- infra/nango/migrations/053_fx_conflict_resolutions.sql
--
-- Story 13.2: MDM conflicts + FX binding
--
-- AD-6: la résolution CURRENCY_CONFLICT ne convertit PAS -- elle déclare quelle devise
-- source utiliser dans le JOIN FX dbt (staging stg_*_daily.sql).
-- AD-8: Postgres est le seul écrivain ; la table est mirrorée vers DuckDB via mirror_sync.
--
-- GRAIN: (project_id, target_field, source_module) -- une résolution par champ par module.
-- Exemple: cost / meta-ads / resolved_source_currency = 'USD' (forçage explicite)
--
-- NON RETEROACTIF: une resolution s'applique aux nouvelles publications/reprocess uniquement.
-- Elle n'est PAS retropropagee sur des materialisations passees. Le mecanisme est emergent :
-- les tables dbt deja materialisees ne sont pas recrites ; la resolution prend effet lors du
-- prochain `dbt build` / reprocess. Il n'existe PAS de trigger de protection : la contrainte
-- de non-retroactivite est documentaire et operationnelle (les commits passes restent inchanges
-- car dbt ne les re-materialise pas sans un reprocess explicite). Le champ decided_at marque
-- la date d'effet ; le staging dbt consomme la table a la prochaine materialisation.
-- Aucun code Python ne convertit une devise (AD-6).
--
-- AUDITABILITÉ: append-only par convention (pas de DELETE dans le workflow normal) ;
-- decided_at + decided_by tracent la décision.

-- ---------------------------------------------------------------------------
-- 1. Table principale
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.fx_conflict_resolutions (
    id                    BIGSERIAL PRIMARY KEY,
    project_id            TEXT NOT NULL,
    target_field          TEXT NOT NULL,
    source_module         TEXT NOT NULL,    -- module_name (ex: 'meta-ads', 'tiktok-ads')
    resolved_source_currency  TEXT NOT NULL,   -- devise source déclarée (ex: 'USD', 'EUR')
    decided_by            TEXT NOT NULL,
    decided_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    note                  TEXT,             -- justification libre (optionnel)

    CONSTRAINT fx_conflict_resolutions_uq
        UNIQUE (project_id, target_field, source_module)
);

COMMENT ON TABLE app.fx_conflict_resolutions IS
    'Résolutions de conflits CURRENCY_CONFLICT : déclare quelle devise source utiliser '
    'par (projet, champ cible, module). Consommée par les modèles dbt staging via JOIN '
    'sur mirror.fx_conflict_resolutions. AD-6 : AUCUNE conversion en Python.';

COMMENT ON COLUMN app.fx_conflict_resolutions.resolved_source_currency IS
    'Devise source à utiliser pour ce module/champ/projet. Remplace cost_source_currency '
    'dans le JOIN FX dbt : COALESCE(res.resolved_source_currency, raw.cost_source_currency).';

COMMENT ON COLUMN app.fx_conflict_resolutions.source_module IS
    'module_name du datastream concerné (valeur de app.datastreams.module_name). '
    'Source-agnostique : pas de constante codée en dur (AD-2).';

-- ---------------------------------------------------------------------------
-- 2. Index pour les lookups dbt (project_id, source_module)
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_fx_conflict_res_project_module
    ON app.fx_conflict_resolutions (project_id, source_module);

CREATE INDEX IF NOT EXISTS idx_fx_conflict_res_field
    ON app.fx_conflict_resolutions (target_field);

-- ---------------------------------------------------------------------------
-- 3. Table d'audit des résolutions MEASURE_NULL
--    (déclaration explicite de measure sur un champ sans measure)
--    Réutilise l'API update_target_field existante -> pas de table dédiée.
--    Cette section documente la décision : les résolutions MEASURE_NULL sont
--    des PATCH sur app.target_fields.measure via l'endpoint existant PATCH
--    /api/datamodel/fields/{name}. Pas de table supplémentaire (DRY).
-- ---------------------------------------------------------------------------

-- (intentionnellement vide -- voir update_target_field dans datamodel.py)

-- ---------------------------------------------------------------------------
-- 4. Vue miroir simplifiée (utile pour dbt source)
-- ---------------------------------------------------------------------------

-- Pas de vue Postgres nécessaire : mirror_sync copie la table brute vers DuckDB.
-- Le staging dbt fait le JOIN directement sur mirror.fx_conflict_resolutions.
