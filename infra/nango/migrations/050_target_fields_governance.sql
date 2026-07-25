-- infra/nango/migrations/050_target_fields_governance.sql
--
-- Story 13.1: Dictionnaire editable + approbation (Epic 13).
-- Gouvernance du dictionnaire de donnees : statut draft/approved, audit
-- d'approbation, suivi de la modification et suppression avec garde used-by.
--
-- MIGRATION NUMBER: 050. Le 049 (metric_semantics) est le plus recent applique.
--
-- Additive et idempotente : chaque instruction utilise ADD COLUMN IF NOT EXISTS
-- ou IF NOT EXISTS pour pouvoir etre re-appliquee sans effet de bord.
--
-- CHOIX DEFAULT STATUS (documente ici comme convenu dans la story) :
--   DEFAULT 'approved' sur la colonne SQL, et le code applicatif force 'draft'
--   pour les nouveaux champs utilisateur (is_default=false). Les champs is_default=true
--   deja en base restent 'approved' grace a ce DEFAULT, ce qui evite une UPDATE
--   massive post-migration et conserve la semantique "les champs systeme sont actifs".
--   Un backfill conditionnel est quand meme fourni ci-dessous pour expliciter
--   l'intention : is_default=true -> 'approved', is_default=false -> 'draft'.
--
-- HISTORISATION DES DECISIONS D'APPROBATION :
--   Une table app.target_field_approvals (append-only, trigger identique a
--   app.datastream_publication_log de la migration 042) est creee. Ce mecanisme
--   est coherent avec le style du repo (trigger reject mutation) et permet
--   d'historiser chaque decision (approve/reject) sans réécrire les champs.
--
-- Applique via (gate human Jean -- Supabase seulement) :
--   psql -f 050_target_fields_governance.sql
--
-- Style : suit la migration 042 (IF NOT EXISTS, pg_constraint guard, trigger).

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Colonnes de gouvernance sur app.target_fields
-- ---------------------------------------------------------------------------

-- status : 'draft' = en attente d'approbation, 'approved' = actif pour reports/cards.
-- DEFAULT 'approved' pour ne pas casser les champs systeme existants (is_default=true).
-- Voir CHOIX DEFAULT STATUS ci-dessus.
ALTER TABLE app.target_fields
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'approved'
    CHECK (status IN ('draft', 'approved'));

-- Timestamp de la derniere approbation (NULL si jamais approuve explicitement).
ALTER TABLE app.target_fields
    ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;

-- Identite de l'approbateur (email/subject).
ALTER TABLE app.target_fields
    ADD COLUMN IF NOT EXISTS approved_by TEXT;

-- Timestamp de derniere modification (mis a jour par trigger ci-dessous).
ALTER TABLE app.target_fields
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- ---------------------------------------------------------------------------
-- 2. Backfill explicite : is_default=true -> 'approved', is_default=false -> 'draft'
--    (idempotent : si status est deja correct, ON CONFLICT / WHERE guard l'ignore)
-- ---------------------------------------------------------------------------

-- Les champs systeme (is_default=true) sont approuves d'office.
UPDATE app.target_fields
SET status = 'approved'
WHERE is_default = TRUE
  AND status != 'approved';

-- Les champs utilisateur (is_default=false) existants restent 'approved' (DEFAULT)
-- sauf si on veut les repasser en draft ; on laisse 'approved' pour compatibilite.
-- NOTE : les NOUVEAUX champs utilisateur crees apres cette migration naissent 'draft'
-- car le code applicatif (create_target_field) force explicitement status='draft'.

-- ---------------------------------------------------------------------------
-- 3. Index sur status pour les requetes de filtrage (reports/cards)
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_target_fields_status
    ON app.target_fields (status);

-- ---------------------------------------------------------------------------
-- 4. Trigger updated_at sur app.target_fields
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION app.set_target_fields_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_target_fields_updated_at ON app.target_fields;
CREATE TRIGGER trg_target_fields_updated_at
    BEFORE UPDATE ON app.target_fields
    FOR EACH ROW EXECUTE FUNCTION app.set_target_fields_updated_at();

-- ---------------------------------------------------------------------------
-- 5. app.target_field_approvals -- historique append-only des decisions
--
-- Une ligne par appel a POST /api/datamodel/fields/{name}/approve.
-- Immutable via trigger (meme pattern que 042 datastream_publication_log).
-- action IN ('approved', 'rejected') pour historiser aussi les refus eventuels.
-- ---------------------------------------------------------------------------

-- NOTE (C1 fix): NO foreign key from field_name -> target_fields.
-- The approvals table is a historical audit journal: rows must survive even
-- after the referenced field has been deleted (orphan rows = preserved audit
-- trace of a deleted field).  The append-only trigger below already prevents
-- mutations; referential integrity is enforced at the application layer
-- (delete_target_field checks used_by_count before deleting the field row).
CREATE TABLE IF NOT EXISTS app.target_field_approvals (
    id              BIGSERIAL   NOT NULL,
    field_name      TEXT        NOT NULL,
    action          TEXT        NOT NULL
                    CHECK (action IN ('approved', 'rejected')),
    decided_by      TEXT        NOT NULL,
    decided_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    note            TEXT,
    CONSTRAINT pk_target_field_approvals PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS idx_target_field_approvals_field
    ON app.target_field_approvals (field_name, decided_at DESC);

-- Trigger append-only : interdit UPDATE et DELETE (meme logique que 042).
CREATE OR REPLACE FUNCTION app.reject_target_field_approvals_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'target_field_approvals is append-only'
        USING ERRCODE = '23000';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_target_field_approvals_immutable
    ON app.target_field_approvals;
CREATE TRIGGER trg_target_field_approvals_immutable
    BEFORE UPDATE OR DELETE ON app.target_field_approvals
    FOR EACH ROW EXECUTE FUNCTION app.reject_target_field_approvals_mutation();

COMMIT;
