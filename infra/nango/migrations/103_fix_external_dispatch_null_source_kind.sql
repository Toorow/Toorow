-- 103_fix_external_dispatch_null_source_kind.sql
--
-- Toute creation de datastream sans `source_kind` explicite repondait 500.
--
-- Le trigger de synchronisation ecrivait :
--
--     NEW.external_dispatch_excluded := (NEW.source_kind = 'external_bq');
--
-- En SQL, une comparaison avec NULL ne rend pas FALSE mais NULL. Or
-- `source_kind` est optionnel -- `create_datastream` le laisse a NULL pour un
-- flux de collecte classique -- et `external_dispatch_excluded` est NOT NULL
-- (migration 076). Le trigger produisait donc NULL et violait sa propre
-- colonne, sur le chemin le PLUS courant : le datastream ordinaire.
--
-- `IS NOT DISTINCT FROM` compare en traitant NULL comme une valeur : NULL rend
-- FALSE, ce qui est la reponse voulue -- un flux sans source_kind n'est pas un
-- flux external_bq, donc il n'est pas exclu du dispatch.

BEGIN;

CREATE OR REPLACE FUNCTION app.sync_external_dispatch_excluded()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    NEW.external_dispatch_excluded := (NEW.source_kind IS NOT DISTINCT FROM 'external_bq');
    RETURN NEW;
END;
$function$;

COMMIT;
