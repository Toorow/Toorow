-- infra/nango/migrations/051_render_snapshots.sql
--
-- Story 13.5 volet (a) : galerie des rendus (NON gatee).
--
-- Persistance des snapshots d'envelope au moment du rendu (hook serveur sur
-- get_card / get_report, scope projet, retention bornee configurable).
--
-- TABLE principale : app.render_snapshots
--   - id          : ULID prefixe 'rsn_' (PK)
--   - project_id  : FK -> app.projects (ON DELETE CASCADE -- si le projet est
--                   supprime, ses snapshots disparaissent aussi)
--   - tool_name   : 'get_card' ou 'get_report'
--   - tool_args   : JSONB -- arguments passes a l'outil (template, report_ref, etc.)
--   - envelope    : JSONB -- envelope AD-1 complete gelee au moment du rendu
--                   (schema_version, meta:{freshness,provenance,alerts,...}, data:{...})
--                   Aucune transformation : AD-9 (fraicheur/provenance preservees telles quelles)
--   - widget_uri  : URI du widget MCP au moment du rendu
--   - summary_snippet : extrait du summary LLM (truncation a 500 caracteres)
--   - question    : la question/report (answers_question du template ou report_id)
--   - identity    : sujet OAuth (sub / client_id) de l'appelant
--   - trace_id    : identifiant de trace distribuee (hex)
--   - created_at  : horodatage du rendu (serveur)
--
-- RETENTION configurable :
--   La purge s'execute a chaque write. Elle supprime les snapshots au-dela de
--   RENDER_SNAPSHOT_RETENTION_DAYS (variable d'environnement, defaut 30) jours
--   pour le projet concerne. Pas de tache planifiee -- purge inline, best-effort.
--   Alternative non retenue : garder N derniers par projet (trop rigide, la
--   retention temporelle est plus naturelle pour un historique de rendus).
--
-- SCOPING AD-5 : toute lecture est filtree par project_id.
--   Les endpoints REST refusent avec 404 non-disclosant si l'identite n'a pas
--   acces au projet (identity_has_project_access, meme pattern que les notebooks).
--
-- INDEXES :
--   (project_id, created_at DESC) pour la liste chronologique par projet.
--   (project_id, tool_name, created_at DESC) pour filtrer par outil.
--
-- STRICTEMENT ADDITIF : aucune table existante n'est modifiee.
-- IDEMPOTENT : CREATE TABLE IF NOT EXISTS + DROP CONSTRAINT IF EXISTS.
--

BEGIN;

CREATE TABLE IF NOT EXISTS app.render_snapshots (
    id               TEXT        PRIMARY KEY,
    project_id       TEXT        NOT NULL,
    tool_name        TEXT        NOT NULL
                     CHECK (tool_name IN ('get_card', 'get_report')),
    tool_args        JSONB,
    envelope         JSONB       NOT NULL,
    widget_uri       TEXT,
    summary_snippet  TEXT,
    question         TEXT,
    identity         TEXT,
    trace_id         TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Index principal pour la galerie (liste chronologique par projet).
CREATE INDEX IF NOT EXISTS render_snapshots_project_created
    ON app.render_snapshots (project_id, created_at DESC);

-- Index secondaire pour le filtre par outil.
CREATE INDEX IF NOT EXISTS render_snapshots_project_tool_created
    ON app.render_snapshots (project_id, tool_name, created_at DESC);

-- FK vers app.projects : CASCADE (si le projet est supprime, ses snapshots aussi).
ALTER TABLE app.render_snapshots
    DROP CONSTRAINT IF EXISTS fk_render_snapshots_project;
ALTER TABLE app.render_snapshots
    ADD CONSTRAINT fk_render_snapshots_project
    FOREIGN KEY (project_id) REFERENCES app.projects(id) ON DELETE CASCADE;

COMMIT;
