-- infra/nango/migrations/061_daily_insights.sql
--
-- Epic 35 (Story 35.3) : persistance JSONB des runs et insights agentiques quotidiens.
--
-- Gate 35.0 a tranche A (le client LLM SELECTIONNE une card existante). Les insights
-- publies sont des artefacts metier DURABLES : le payload gele l'envelope resolue ET
-- l'insight editorial, de sorte qu'il reste partageable meme si le run LLM d'origine et
-- la conversation disparaissent (Epic 35 sec.7). La galerie « Rendus » (app.render_snapshots)
-- a une retention bornee ; ces deux tables N'ONT PAS de purge -- ce sont des artefacts durables.
--
-- TABLE app.daily_insight_runs : UN run par (project_id, insight_date).
--   - status distingue explicitement les etats : 'published' | 'no_insight' | 'blocked'
--     | 'failed'. Un run 'no_insight' (0 item) reste distinct d'un run 'blocked'
--     (donnees J-1 non pretes), d'un run 'failed', et d'un run ABSENT (« la tache n'a
--     pas tourne »). Ces trois derniers ne doivent jamais etre confondus.
--   - period_from/period_to : la fenetre reellement analysee.
--   - coverage : manifeste de couverture (datastreams vus, fraicheur, DQ) au moment du run.
--   - host / prompt_version / contract_version : provenance de la recherche agentique
--     (client LLM, version du prompt, SCHEMA_VERSION du contrat 35.0).
--
-- TABLE app.daily_insights : 0 a 3 items par run.
--   - slot : 0..2, unique par (project_id, insight_date, slot) -> idempotence de publication.
--   - payload : JSONB AUTONOME (insight editorial + envelope de card figee).
--   - payload_hash : SHA-256 canonique (daily_insights_schema.canonical_payload_hash).
--   - render_snapshot_id : lignage vers app.render_snapshots (FK SET NULL -- si le snapshot
--     est purge par retention, l'artefact durable n'est PAS casse).
--
-- SCOPING AD-5 : project_id est denormalise sur daily_insights pour que toute lecture
--   soit filtree par projet sans jointure (deni cross-projet non-disclosant).
--
-- STRICTEMENT ADDITIF. IDEMPOTENT (CREATE TABLE IF NOT EXISTS + DROP CONSTRAINT IF EXISTS).
--

BEGIN;

CREATE TABLE IF NOT EXISTS app.daily_insight_runs (
    id                TEXT        PRIMARY KEY,          -- 'dir_<ulid>'
    project_id        TEXT        NOT NULL,
    insight_date      DATE        NOT NULL,
    status            TEXT        NOT NULL
                      CHECK (status IN ('published', 'no_insight', 'blocked', 'failed')),
    period_from       DATE,
    period_to         DATE,
    coverage          JSONB,
    host              TEXT,
    prompt_version    TEXT,
    contract_version  TEXT,
    identity          TEXT,
    trace_id          TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_daily_insight_runs_project_date UNIQUE (project_id, insight_date)
);

CREATE INDEX IF NOT EXISTS daily_insight_runs_project_date
    ON app.daily_insight_runs (project_id, insight_date DESC);

CREATE TABLE IF NOT EXISTS app.daily_insights (
    id                 TEXT        PRIMARY KEY,         -- 'din_<ulid>'
    run_id             TEXT        NOT NULL,
    project_id         TEXT        NOT NULL,            -- denormalise pour scoping AD-5
    insight_date       DATE        NOT NULL,
    slot               SMALLINT    NOT NULL
                       CHECK (slot >= 0 AND slot <= 2),
    payload            JSONB       NOT NULL,
    payload_hash       TEXT        NOT NULL,
    render_snapshot_id TEXT,
    identity           TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_daily_insights_project_date_slot UNIQUE (project_id, insight_date, slot)
);

CREATE INDEX IF NOT EXISTS daily_insights_run
    ON app.daily_insights (run_id);
CREATE INDEX IF NOT EXISTS daily_insights_project_date
    ON app.daily_insights (project_id, insight_date DESC);

-- FK run -> app.projects : si le projet disparait, ses runs aussi.
ALTER TABLE app.daily_insight_runs
    DROP CONSTRAINT IF EXISTS fk_daily_insight_runs_project;
ALTER TABLE app.daily_insight_runs
    ADD CONSTRAINT fk_daily_insight_runs_project
    FOREIGN KEY (project_id) REFERENCES app.projects(id) ON DELETE CASCADE;

-- FK item -> run : cascade sur suppression du run.
ALTER TABLE app.daily_insights
    DROP CONSTRAINT IF EXISTS fk_daily_insights_run;
ALTER TABLE app.daily_insights
    ADD CONSTRAINT fk_daily_insights_run
    FOREIGN KEY (run_id) REFERENCES app.daily_insight_runs(id) ON DELETE CASCADE;

-- FK item -> render_snapshot : SET NULL (le snapshot a une retention bornee, l'artefact
-- durable ne doit jamais casser quand le snapshot de lignage est purge).
ALTER TABLE app.daily_insights
    DROP CONSTRAINT IF EXISTS fk_daily_insights_snapshot;
ALTER TABLE app.daily_insights
    ADD CONSTRAINT fk_daily_insights_snapshot
    FOREIGN KEY (render_snapshot_id) REFERENCES app.render_snapshots(id) ON DELETE SET NULL;

COMMIT;
