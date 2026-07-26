-- 106_dimension_labels.sql
--
-- Story 27.9 -- le libelle appartient au client.
--
-- `canonical_dimension` (migration 052) est deja decrit comme "a client-chosen
-- conformed-dimension label", avec la meme cascade PROJECT > ORG > PLATFORM que
-- 049/052. Le modele etait la ; ce qui manquait, c'est le libelle LUI-MEME :
-- l'identifiant conforme est un identifiant STABLE (il est ecrit dans les lignes
-- de mapping, dans les payloads de plan, dans les cartes conservees) et il ne
-- peut donc pas etre renomme a la demande. Le renommer serait casser la lignee.
--
-- Cette table separe les deux roles, une bonne fois :
--
--   * canonical_dimension -- l'IDENTIFIANT stable, interne, jamais affiche.
--   * display_label       -- LE NOM DU CLIENT, ce que l'utilisateur lit, libre,
--                            surchargeable par ORG puis par PROJET.
--
-- Meme doctrine que l'id de marche stable sous un label libre (epic-37) : on ne
-- renomme jamais une cle, on lui attache un libelle.
--
-- ABSENCE DE LIBELLE = ABSENCE, PAS INVENTION : quand aucune ligne ne couvre une
-- dimension, la resolution retombe HONNETEMENT sur l'identifiant et le declare
-- (`label_source = 'fallback_identifier'`). Rien n'est fabrique cote lecture.
--
-- SCOPING : le triplet (scope_level, org_id, project_id) est repete a l'identique
-- de 049/052 -- PLATFORM par defaut, ORG ancre, PROJECT exception. Les FK
-- ON DELETE CASCADE font que la disparition d'une org / d'un projet emporte ses
-- libelles ; les defauts PLATFORM survivent. La table est MUTABLE (un libelle se
-- corrige) : aucun garde append-only n'est pose dessus, donc rien a ajouter a
-- l'allowlist RGPD de la migration 099 -- l'effacement d'org passe par la FK,
-- decouverte par le graphe de core/org_purge.py.
--
-- AUDIT : pas de nouvelle table d'audit. Le registre append-only
-- app.metric_semantics_audit (049) a une colonne entity_type TEXT libre ; 27.9 y
-- ecrit avec entity_type='dimension_label' via le meme _write_semantics_audit.
-- Dependance SOUPLE sur 049 (sa table d'audit doit exister).
--
-- ID prefixe (prefixed-ULID) : 'dlb_'.
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/106_dimension_labels.sql
--
-- Schema-Change-Checklist (CONTRIBUTING.md):
--   [x] Additive & idempotente (IF NOT EXISTS partout ; le fichier est rejouable)
--   [x] Nouvelles colonnes NULL-ables ou avec defaut
--   [x] Aucun DROP/ALTER destructif sur une colonne peuplee
-- Numero 106 : 101-105 sont pris. Renumerotee depuis 103 apres le merge sur main,
-- ou une 103_fix_external_dispatch_null_source_kind.sql existait deja (collision).

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- ---------------------------------------------------------------------------
-- app.dimension_labels -- (canonical_dimension, scope) -> libelle client.
--
-- Contrat du triplet de portee (repete a l'identique de 049/052) :
--   PLATFORM => org_id IS NULL     AND project_id IS NULL
--   ORG      => org_id IS NOT NULL AND project_id IS NULL
--   PROJECT  => project_id IS NOT NULL   (org_id OPTIONNEL : le projet porte deja
--               son org dans app.projects, cf. note 049)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.dimension_labels (
    id                   TEXT        PRIMARY KEY,          -- ULID prefixe : 'dlb_'
    canonical_dimension  TEXT        NOT NULL,             -- identifiant STABLE, jamais affiche
    display_label        TEXT        NOT NULL,             -- ce que l'utilisateur lit
    description          TEXT,                             -- glose optionnelle (surface LLM)
    scope_level          TEXT        NOT NULL CHECK (scope_level IN ('PLATFORM','ORG','PROJECT')),
    org_id               TEXT        REFERENCES app.organizations(id) ON DELETE CASCADE,
    project_id           TEXT        REFERENCES app.projects(id)      ON DELETE CASCADE,
    created_by           TEXT        NOT NULL,             -- sujet d'identite (AD-14)
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_dimension_labels_scope_cols CHECK (
        (scope_level = 'PLATFORM' AND org_id IS NULL     AND project_id IS NULL)
     OR (scope_level = 'ORG'      AND org_id IS NOT NULL AND project_id IS NULL)
     OR (scope_level = 'PROJECT'  AND project_id IS NOT NULL)
    ),
    CONSTRAINT ck_dimension_labels_not_blank CHECK (
        length(btrim(canonical_dimension)) > 0 AND length(btrim(display_label)) > 0
    )
);

-- UNICITE : un seul libelle par (portee, dimension). Le COALESCE(...,'') est
-- OBLIGATOIRE (NULL <> NULL laisserait passer deux lignes PLATFORM). Meme
-- discipline que 049 et 052.
CREATE UNIQUE INDEX IF NOT EXISTS uq_dimension_labels_scope_key
    ON app.dimension_labels
    (scope_level, COALESCE(org_id, ''), COALESCE(project_id, ''), canonical_dimension);

-- Chemin de resolution (cascade PROJECT > ORG > PLATFORM sur une dimension).
CREATE INDEX IF NOT EXISTS ix_dimension_labels_resolve
    ON app.dimension_labels (canonical_dimension, scope_level);

-- Trigger updated_at (reutilise app.set_updated_at() de la migration 023).
DROP TRIGGER IF EXISTS trg_dimension_labels_updated_at ON app.dimension_labels;
CREATE TRIGGER trg_dimension_labels_updated_at
    BEFORE UPDATE ON app.dimension_labels
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

COMMENT ON TABLE app.dimension_labels IS
    'Story 27.9: libelle possede par le client pour une dimension conforme. canonical_dimension = identifiant STABLE interne (jamais affiche) ; display_label = ce que l''utilisateur lit. Cascade PROJECT > ORG > PLATFORM (le plus specifique gagne). Absence de ligne => la lecture retombe sur l''identifiant et le declare (fallback_identifier), jamais un libelle invente. Mutations auditees dans app.metric_semantics_audit (049) avec entity_type=dimension_label.';

COMMIT;
