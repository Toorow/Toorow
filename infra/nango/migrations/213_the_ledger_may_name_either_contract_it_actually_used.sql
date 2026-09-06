-- 213_the_ledger_may_name_either_contract_it_actually_used.sql
--
-- LA CHAINE FICHIER-SOURCE N'A JAMAIS PU ATTERRIR SUR UN VRAI POSTGRES, et rien
-- ne le disait. `app.managed_feed_import_ledger.import_contract_id` porte une
-- cle etrangere vers `app.csv_excel_import_contracts`, posee par la migration
-- 078 -- ecrite AVANT que l'epic 22 existe, quand ce chemin etait le seul.
--
-- La story 22.12 a ensuite decide l'inverse, et le code le dit noir sur blanc
-- (`server/core/csv_excel_import.py`, branche `producer is not None`) :
--   « the versioned contract is the file-source template (Story 22.11), not a
--     CSV/Excel contract [...] never version a CSV/Excel contract here »
-- Le chemin fichier-source ecrit donc un `fst_...` dans une colonne contrainte
-- a `cic_...`, et chaque arrivee se termine par
--
--     ForeignKeyViolation: la cle (import_contract_id)=(fst_...) n'est pas
--     presente dans la table « csv_excel_import_contracts »
--
-- Pourquoi personne ne l'a vu : story-log.md, 2026-07-25, story 22.12 --
-- « VERIF (central, offline) [...] import_contract_id threaded=fst_id ». La
-- verification etait OFFLINE, ou aucune cle etrangere n'existe. Mesure ici le
-- 2026-08-05 en semant enfin la confirmation epic-22 dans le harnais d'armement
-- (AI-187) : la confirmation passait, et le mur suivant etait celui-ci.
--
-- LA COLONNE N'EST PAS RELACHEE, elle est rendue POLYMORPHE ET VERIFIEE. Se
-- contenter de supprimer la cle etrangere ferait passer n'importe quelle chaine
-- de caracteres, y compris un contrat d'un autre projet : c'est precisement
-- l'integrite que la 078 protegeait, et elle vaut pour les deux chemins. Le
-- declencheur ci-dessous exige que l'id existe REELLEMENT, dans la table que son
-- prefixe designe -- meme idiome que la validation de la migration 188.

BEGIN;

ALTER TABLE app.managed_feed_import_ledger
    DROP CONSTRAINT IF EXISTS managed_feed_import_ledger_import_contract_id_fkey;

COMMENT ON COLUMN app.managed_feed_import_ledger.import_contract_id IS
    'The immutable contract this import actually ran under. Polymorphic by '
    'prefix and validated by trigger: cic_ -> app.csv_excel_import_contracts '
    '(the 12.9 CSV/Excel path), fst_ -> app.file_source_templates (the epic-22 '
    'file-source path, where the Template IS the parsing contract). NULL for '
    'non-CSV/Excel feeds and for 12.8 direct imports that predate 12.9.';

CREATE OR REPLACE FUNCTION app.validate_import_ledger_contract_ref()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    referenced BOOLEAN;
BEGIN
    IF NEW.import_contract_id IS NULL THEN
        RETURN NEW;
    END IF;

    IF NEW.import_contract_id LIKE 'cic\_%' THEN
        SELECT EXISTS (
            SELECT 1 FROM app.csv_excel_import_contracts
            WHERE id = NEW.import_contract_id
        ) INTO referenced;
    ELSIF NEW.import_contract_id LIKE 'fst\_%' THEN
        SELECT EXISTS (
            SELECT 1 FROM app.file_source_templates
            WHERE id = NEW.import_contract_id
        ) INTO referenced;
    ELSE
        RAISE EXCEPTION
            'import_contract_id % names no known contract family (expected cic_ or fst_)',
            NEW.import_contract_id;
    END IF;

    IF NOT referenced THEN
        RAISE EXCEPTION
            'import_contract_id % does not exist in the table its prefix names',
            NEW.import_contract_id;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_import_ledger_contract_ref
    ON app.managed_feed_import_ledger;
CREATE TRIGGER trg_import_ledger_contract_ref
    BEFORE INSERT OR UPDATE OF import_contract_id ON app.managed_feed_import_ledger
    FOR EACH ROW
    EXECUTE FUNCTION app.validate_import_ledger_contract_ref();

COMMIT;
