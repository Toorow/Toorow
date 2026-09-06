-- Une selection de compte etait une FEUILLE ; plusieurs fournisseurs exigent la
-- BRANCHE.
--
-- `app.connection_account_scope` ne portait que `account_id`. Or quatre
-- connecteurs declarent une hierarchie et ne peuvent pas agir sur son extremite
-- sans l'ancetre sous lequel l'appel est emis -- mesure du 2026-08-11, lue dans
-- leurs propres manifestes :
--
--   google-ads         manager > client_account          login-customer-id = le MANAGER
--   dv360              partner > advertiser              partnerId         = le PARTNER
--   cm360              user_profile > ... > advertiser   profileId         = le USER_PROFILE
--   sa360              accessible > manager_login > ...  login-customer-id = le MANAGER_LOGIN
--
-- Le compte fils d'un MCC -- le cas ORDINAIRE d'une agence -- pouvait donc etre
-- choisi et jamais interroge : `discover_accounts` marchait l'arbre, le coeur
-- n'en gardait que les identifiants (`_flatten_account_ids`), et la branche
-- etait perdue avant d'atteindre `pull()`.
--
-- `selection_path` porte l'ancetrie telle que la decouverte l'a rendue, racine
-- d'abord : [{"id": ..., "label": ...}, ...]. Le coeur ne connait AUCUN nom de
-- niveau (AD-2) -- c'est le manifeste qui dit quel niveau alimente quel
-- parametre, dans `account_topology.operating_context`.
--
-- DEFAUT VIDE et non NULL : un compte prouve par `verification` n'a pas
-- d'ancetrie a lire (le fournisseur ne l'enumere pas), et une liste vide dit
-- exactement cela sans obliger chaque lecteur a distinguer deux absences.

ALTER TABLE app.connection_account_scope
    ADD COLUMN IF NOT EXISTS selection_path JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE app.connection_account_scope
    DROP CONSTRAINT IF EXISTS ck_connection_account_scope_selection_path;

ALTER TABLE app.connection_account_scope
    ADD CONSTRAINT ck_connection_account_scope_selection_path
    CHECK (jsonb_typeof(selection_path) = 'array');
