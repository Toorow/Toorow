-- 292 -- Le ledger d'import managed_feed porte une politique RLS (audit C28).
--
-- CE QUI A ETE MESURE, 2026-08-20, par lecture des migrations du depot :
-- `app.managed_feed_import_ledger` et `app.managed_feed_rejected_rows`
-- (migration 077) sont les deux tables de la famille inbound qui n'ont AUCUNE
-- politique RLS -- `inbound_receipts` (184), `inbound_raw_imports` (185),
-- `inbound_scan_jobs` (186), `csv_excel_import_contracts` (192),
-- `managed_file_dispatches` (194) en portent une. Le code scope chaque requete
-- par `project_id`, mais la table qui porte outcomes, content_hash et
-- relations d'atterrissage est precisement celle qui n'a pas le garde DB.
--
-- POURQUOI ELLES ONT ECHAPPE A 273. La migration 273 arme les tables portant
-- `org_id` ; ces deux-la n'en ont pas -- elles adressent leur portee par
-- `datastream_id` (+ `project_id`), la forme exacte d'`inbound_receipts`. Le
-- predicat est donc celui de 184/185, pas celui de 273 : la portee 'flux' via
-- la jointure `app.datastreams`, parce que `epic36_has_resource_access` exige
-- un (scope_type, scope_id) et que 'flux' est le scope d'un datastream.
--
-- CE QUE CETTE MIGRATION NE CHANGE PAS. Comme pour 273 et 184/185, le
-- predicat reste conditionne par `toorow.enforce_epic36` : les chemins de fond
-- (background_connection -- scheduler, queue, worker d'import) ne l'arment
-- pas et lisent exactement les memes lignes qu'avant. La logique metier reste
-- la premiere barriere ; ceci est la seconde, et elle etait absente sur ces
-- deux tables, pas cassee.
--
-- RESTE UN ECART PLUS LARGE, MESURE ET TRACE (audit C33) : la famille des
-- tables scopees project/datastream SANS colonne `org_id` et SANS politique
-- ne se reduit pas a ces deux tables (une centaine lues statiquement, dont
-- `pull_jobs`, `alert_firings`, `connection_ref`). Leur armement exige la
-- meme lecture table par table que 273 et une mesure sur Postgres reel -- ce
-- n'est pas un correctif a ecrire a l'aveugle. Le constat et le chemin de
-- decision vivent dans `_bmad-output/qa/audit-flux-complet-2026-08-20.md`.

BEGIN;

ALTER TABLE app.managed_feed_import_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.managed_feed_import_ledger FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS managed_feed_import_ledger_strict ON app.managed_feed_import_ledger;
CREATE POLICY managed_feed_import_ledger_strict ON app.managed_feed_import_ledger
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR EXISTS (
            SELECT 1 FROM app.datastreams d
            WHERE d.id = datastream_id
              AND app.epic36_has_resource_access(d.org_id, 'flux', d.id)
        )
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR EXISTS (
            SELECT 1 FROM app.datastreams d
            WHERE d.id = datastream_id
              AND app.epic36_has_resource_access(d.org_id, 'flux', d.id)
        )
    );

ALTER TABLE app.managed_feed_rejected_rows ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.managed_feed_rejected_rows FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS managed_feed_rejected_rows_strict ON app.managed_feed_rejected_rows;
CREATE POLICY managed_feed_rejected_rows_strict ON app.managed_feed_rejected_rows
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR EXISTS (
            SELECT 1 FROM app.datastreams d
            WHERE d.id = datastream_id
              AND app.epic36_has_resource_access(d.org_id, 'flux', d.id)
        )
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR EXISTS (
            SELECT 1 FROM app.datastreams d
            WHERE d.id = datastream_id
              AND app.epic36_has_resource_access(d.org_id, 'flux', d.id)
        )
    );

COMMIT;
