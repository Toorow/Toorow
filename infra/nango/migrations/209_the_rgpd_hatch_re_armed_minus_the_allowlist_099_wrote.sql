-- 209 -- l'effacement d'organisation est REDEVENU impossible, sur 39 tables
--
-- ⚠️ LIRE LE BLOC « CE QUE LA PREMIERE VERSION A CASSE » EN BAS AVANT DE
-- REPRENDRE CE FICHIER. Un balayage qui arme « tout ce qui n'est pas arme »
-- desarme aussi les exclusions que quelqu'un avait posees expres.
--
-- La migration 200 (2026-08-03) a armé l'échappatoire RGPD sur 62 gardes
-- append-only, et elle a écrit la doctrine qui va avec, reprise dans SESSIONS.md :
-- « Si vous ajoutez une table append-only, son trigger doit porter
-- l'échappatoire, sinon il rebloque l'effacement d'organisation. »
--
-- Elle n'a pas été suivie. TROUVÉ EN TIRANT, pas en auditant : une purge
-- d'organisation lancée le 2026-08-04 s'est arrêtée sur
--
--   psycopg.errors.RaiseException: Business taxonomy history is append-only:
--   DELETE blocked
--   CONTEXT: PL/pgSQL function app.reject_business_taxonomy_version_mutation()
--
-- MESURÉ ENSUITE, et le nombre n'est pas 1 mais 39. Le plan de purge de cette
-- organisation traverse 175 tables en 2 236 opérations ; 39 d'entre elles portent
-- une garde append-only sans échappatoire, ni dans la clause WHEN du trigger ni
-- dans le corps de sa fonction :
--
--   -- les tables du plan (core.org_purge.plan_purge) croisées avec :
--   SELECT DISTINCT cl.relname FROM pg_trigger tg
--     JOIN pg_class cl ON cl.oid = tg.tgrelid
--     JOIN pg_proc p ON p.oid = tg.tgfoid
--    WHERE NOT tg.tgisinternal AND cl.relnamespace = 'app'::regnamespace
--      AND (tg.tgtype & 8) <> 0 AND (tg.tgtype & 2) <> 0
--      AND pg_get_functiondef(p.oid) ~* 'RAISE'
--      AND pg_get_triggerdef(tg.oid) !~* 'rgpd_erasure'
--      AND pg_get_functiondef(p.oid) !~* 'rgpd_erasure';
--   -> 39 tables communes (dont datastream_setup_*, inbound_scan_*,
--      mdm_business_*_versions, project_configuration_*, render_share_*)
--
-- POURQUOI CETTE MIGRATION EST LE MÊME BLOC QUE LA 200, MOT POUR MOT. Sa boucle
-- est déjà écrite pour ne toucher QUE ce qui n'est pas armé -- elle exclut un
-- trigger dont la clause WHEN ou le corps de fonction cite déjà `rgpd_erasure`.
-- La rejouer est donc idempotent par construction, et écrire une seconde
-- implémentation donnerait deux réponses à « comment arme-t-on l'échappatoire ».
-- Seuls le message de NOTICE et cet en-tête changent.
--
-- CE QUE ÇA NE CHANGE PAS. Une clause WHEN ne modifie aucune logique : elle
-- décide si le trigger SE DÉCLENCHE. Hors effacement, `app.rgpd_erasure` n'est
-- pas à 'on', la garde tire exactement comme avant, et l'append-only reste vrai.
-- Seul `core/org_purge.py` pose ce réglage, en `SET LOCAL`, le temps d'une
-- transaction.
--
-- CE QUI RESTERA À FAIRE APRÈS, ET QUI N'EST PAS UN SQL. Cette migration est la
-- SECONDE fois qu'on repasse derrière la même doctrine. Une garde de conformance
-- qui refuse une table append-only sans échappatoire vaudrait mieux qu'une
-- troisième migration ; elle n'est pas ici parce qu'elle appartient à la suite de
-- conformance, pas au schéma.

BEGIN;

DO $$
DECLARE
    t RECORD;
    def TEXT;
    cut INT;
    when_at INT;
    head TEXT;
    tail TEXT;
    guard CONSTANT TEXT :=
        '(coalesce(current_setting(''app.rgpd_erasure'', TRUE), ''off'') <> ''on'')';
    armed INT := 0;
BEGIN
    FOR t IN
        SELECT tg.oid, tg.tgname, cl.relname, tg.tgqual IS NOT NULL AS has_when
          FROM pg_trigger tg
          JOIN pg_proc p ON p.oid = tg.tgfoid
          JOIN pg_class cl ON cl.oid = tg.tgrelid
          JOIN pg_namespace n ON n.oid = cl.relnamespace
         WHERE NOT tg.tgisinternal
           AND n.nspname = 'app'
           AND (tg.tgtype & 8) <> 0        -- fires on DELETE
           AND (tg.tgtype & 1) <> 0        -- FOR EACH ROW (WHEN needs this)
           AND tg.tgconstraint = 0         -- not a constraint trigger
           AND p.prokind = 'f'             -- pg_get_functiondef raises on aggregates
           AND pg_get_functiondef(p.oid) ILIKE '%RAISE%'
           -- Both places the hatch can live. Checking only the function body
           -- re-arms a trigger already armed in its WHEN, which raises.
           AND pg_get_functiondef(p.oid) NOT ILIKE '%rgpd_erasure%'
           AND pg_get_triggerdef(tg.oid) NOT ILIKE '%rgpd_erasure%'
           -- LA LISTE D'EXCLUSION DE LA 099, RECOPIEE MOT POUR MOT. Ce sont des
           -- decisions, pas des oublis : `audit_log` est la trace DURABLE de
           -- l'effacement et doit lui survivre (la 098 a deprise sa FK au lieu
           -- de la supprimer) ; les six autres sont des donnees de reference
           -- GLOBALES -- << it does not belong to any tenant, so no org erasure
           -- has any business deleting it >>.
           AND cl.relname <> ALL (ARRAY[
                 'audit_log',
                 'import_templates',
                 'file_source_templates',
                 'target_field_approvals',
                 'context_topics_versions',
                 'procedures_versions',
                 'schema_context_versions'
           ])
    LOOP
        -- `pg_get_triggerdef` renders the whole statement, WHEN included:
        --   CREATE TRIGGER x BEFORE DELETE ON app.t FOR EACH ROW
        --     [WHEN (<expr>)] EXECUTE FUNCTION app.f()
        -- so the guard is injected just before EXECUTE FUNCTION and the rest is
        -- carried across verbatim -- timing, event list, transition tables and
        -- arguments included, none of them re-derived by hand.
        def := pg_get_triggerdef(t.oid);

        cut := position(' EXECUTE FUNCTION ' in def);
        IF cut = 0 THEN
            RAISE EXCEPTION 'migration 209: cannot locate EXECUTE FUNCTION in %', def;
        END IF;
        head := substr(def, 1, cut - 1);
        tail := substr(def, cut);

        IF t.has_when THEN
            when_at := position(' WHEN ' in head);
            head := substr(head, 1, when_at - 1)
                    || ' WHEN (' || substr(head, when_at + 6)
                    || ' AND ' || guard || ')';
        ELSE
            head := head || ' WHEN ' || guard;
        END IF;

        EXECUTE format('DROP TRIGGER %I ON app.%I', t.tgname, t.relname);
        EXECUTE head || tail;
        armed := armed + 1;
    END LOOP;

    RAISE NOTICE 'migration 209: RGPD hatch re-armed on % trigger(s)', armed;
END;
$$;

-- CE QUE LA PREMIERE VERSION A CASSE, ET POURQUOI CE BLOC EXISTE
--
-- La premiere ecriture de cette migration armait << tout ce qui n'est pas arme >>,
-- sans lire la 099. Or la 099 ne balaie pas : elle porte une LISTE EXPLICITE, et
-- elle dit pourquoi chaque absence est voulue. Applique en production le
-- 2026-08-04, ce balayage a donc arme les SEPT tables qu'elle excluait -- dont
-- `app.audit_log`, c'est-a-dire qu'il a rendu la trace de l'effacement
-- supprimable PAR l'effacement. Mesure apres coup : 7 sur 7 portaient
-- l'echappatoire.
--
-- Le bloc ci-dessous la retire de ces sept-la. Il rend donc ce fichier idempotent
-- ET correcteur : sur une base neuve la boucle les saute, sur la production il
-- defait le mal deja fait. C'est la meme faute que j'avais faite deux heures plus
-- tot en revoquant UPDATE/DELETE d'apres les noms de triggers -- un balayage ne
-- distingue pas un oubli d'une decision.

DO $$
DECLARE
    t RECORD;
    def TEXT;
    when_at INT;
    cut INT;
    head TEXT;
    tail TEXT;
    restored INT := 0;
BEGIN
    FOR t IN
        SELECT tg.oid, tg.tgname, cl.relname
          FROM pg_trigger tg
          JOIN pg_class cl ON cl.oid = tg.tgrelid
          JOIN pg_namespace n ON n.oid = cl.relnamespace
         WHERE NOT tg.tgisinternal
           AND n.nspname = 'app'
           AND cl.relname = ANY (ARRAY[
                 'audit_log', 'import_templates', 'file_source_templates',
                 'target_field_approvals', 'context_topics_versions',
                 'procedures_versions', 'schema_context_versions'])
           AND pg_get_triggerdef(tg.oid) ILIKE '%rgpd_erasure%'
    LOOP
        def := pg_get_triggerdef(t.oid);
        cut := position(' EXECUTE FUNCTION ' in def);
        head := substr(def, 1, cut - 1);
        tail := substr(def, cut);
        when_at := position(' WHEN ' in head);
        IF when_at = 0 THEN
            RAISE EXCEPTION 'migration 209: % carries the hatch outside a WHEN', t.tgname;
        END IF;
        -- La clause WHEN entiere s'en va : sur ces sept triggers elle ne portait
        -- QUE l'echappatoire (aucun n'avait de WHEN avant la passe fautive).
        head := substr(head, 1, when_at - 1);
        EXECUTE format('DROP TRIGGER %I ON app.%I', t.tgname, t.relname);
        EXECUTE head || tail;
        restored := restored + 1;
    END LOOP;
    RAISE NOTICE 'migration 209: 099 allowlist restored on % trigger(s)', restored;
END;
$$;

COMMIT;
