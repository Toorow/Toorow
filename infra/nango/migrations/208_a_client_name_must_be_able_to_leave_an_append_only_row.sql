-- 208_a_client_name_must_be_able_to_leave_an_append_only_row.sql
--
-- POURQUOI CETTE MIGRATION EXISTE
--
-- Mesure du 2026-08-04, en base de production :
--
--   SELECT id, template_code, created_by FROM app.file_source_templates;
--   -> fst_01KYDN9VKCDRAV197DXJ4NDVWV | AXA_PLAN | tester | 2026-07-25 22:11 UTC
--
-- Une seule ligne, ecrite par la fixture de tests/integration/test_file_source_template_pg.py,
-- et son `template_code` porte le NOM D'UN CLIENT REEL. C'est deux regles du
-- depot violees a la fois : aucun artefact de test dans la base de production, et
-- aucune reference exterieure -- fixtures comprises.
--
-- Elle n'a pas pu partir parce que la 097 a rendu la table append-only : son
-- trigger refuse tout DELETE et gele `template_code` a l'UPDATE. Et la 099 exclut
-- `file_source_templates` de l'echappatoire `app.rgpd_erasure`, DELIBEREMENT et
-- par ecrit -- << global reference data [...] does not belong to any tenant, so no
-- org erasure has any business deleting it >>. Cette exclusion reste juste : ce
-- n'est pas une erasure d'organisation qu'il faut ici.
--
-- CE QUE CETTE MIGRATION FAIT, ET CE QU'ELLE REFUSE DE FAIRE
--
-- Elle NE SUPPRIME RIEN. Une autorisation permanente n'autorise pas
-- l'irreversible, et une ligne append-only detruite ne revient pas. Ce qui doit
-- partir est le NOM, pas la ligne -- alors la ligne reste, et le nom part.
--
-- Elle ouvre pour cela une echappatoire volontairement plus etroite que celle de
-- la 098 : un GUC `app.identifier_scrub`, pose avec SET LOCAL, qui autorise a
-- changer DEUX colonnes et deux seulement --  `template_code` et le
-- `idempotency_key_hash` qui en derive. `id`, `project_id`, `version`, `kind`,
-- `content_hash`, `contract`, `placement_class`, `grain`, `created_by` et
-- `created_at` restent geles MEME sous l'echappatoire : un nettoyage d'identifiant
-- ne doit pas pouvoir devenir une reecriture d'histoire.
--
-- Le hash est recalcule par la MEME regle que le code Python
-- (`file_source_template.default_idempotency_key` puis sha256), sinon la ligne
-- resterait coherente a l'oeil et incoherente a la prochaine creation idempotente.
--
-- Idempotente : re-jouable, et le scrub ne fait rien si plus aucune ligne ne porte
-- le nom.
--
--   psql "$SUPABASE_DSN" -f infra/nango/migrations/208_a_client_name_must_be_able_to_leave_an_append_only_row.sql

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Le trigger apprend l'echappatoire de nettoyage d'identifiant.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION app.reject_file_source_template_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    scrubbing boolean := current_setting('app.identifier_scrub', true) IS NOT DISTINCT FROM 'on';
BEGIN
    IF TG_OP = 'DELETE' THEN
        -- Inchange, et volontairement sans echappatoire : ce qu'on a eu besoin de
        -- retirer ici est un nom, jamais une version de template.
        RAISE EXCEPTION
            'file_source_templates is append-only: '
            'DELETE of id=% is forbidden (immutable template version).',
            OLD.id;
    END IF;

    -- Les colonnes gelees EN TOUTE CIRCONSTANCE, echappatoire comprise.
    IF NEW.id                   IS DISTINCT FROM OLD.id                   OR
       NEW.project_id           IS DISTINCT FROM OLD.project_id           OR
       NEW.version              IS DISTINCT FROM OLD.version              OR
       NEW.kind                 IS DISTINCT FROM OLD.kind                 OR
       NEW.content_hash         IS DISTINCT FROM OLD.content_hash         OR
       NEW.contract             IS DISTINCT FROM OLD.contract             OR
       NEW.placement_class      IS DISTINCT FROM OLD.placement_class      OR
       NEW.grain                IS DISTINCT FROM OLD.grain                OR
       NEW.created_by           IS DISTINCT FROM OLD.created_by           OR
       NEW.created_at           IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION
            'file_source_templates: identity fields are immutable after insert '
            '(id=%). Only label and is_active may be updated.',
            OLD.id;
    END IF;

    -- `template_code` et le hash qui en derive : geles, SAUF sous l'echappatoire.
    IF (NEW.template_code        IS DISTINCT FROM OLD.template_code OR
        NEW.idempotency_key_hash IS DISTINCT FROM OLD.idempotency_key_hash)
       AND NOT scrubbing
    THEN
        RAISE EXCEPTION
            'file_source_templates: template_code is immutable after insert '
            '(id=%). Set app.identifier_scrub to scrub an identifier.',
            OLD.id;
    END IF;

    RETURN NEW;
END;
$$;

-- ---------------------------------------------------------------------------
-- 2. Le nom du client sort de la base -- la ligne reste.
-- ---------------------------------------------------------------------------

DO $scrub$
DECLARE
    scrubbed integer;
BEGIN
    SET LOCAL app.identifier_scrub = 'on';

    UPDATE app.file_source_templates
       SET template_code        = 'EXAMPLE_PLAN',
           idempotency_key_hash = encode(
               sha256(convert_to(
                   'file_source_template:' || project_id || ':EXAMPLE_PLAN:' || content_hash,
                   'UTF8')),
               'hex')
     WHERE template_code = 'AXA_PLAN';

    GET DIAGNOSTICS scrubbed = ROW_COUNT;
    RAISE NOTICE 'file_source_templates: % identifiant(s) nettoye(s)', scrubbed;
END;
$scrub$;

COMMIT;
