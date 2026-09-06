-- 270 -- Aucune sentinelle ne peut etre membre d'une organisation (AI-183, classe).
--
-- CE QUI A ETE MESURE, 2026-08-17, sur la base de production, juste apres la
-- bascule du DSN sur `connector` (AI-100) :
--
--   SELECT * FROM app.org_members;
--   -> omem_anonymous_toorow | org_01KYJ0NP8VKF4VSC3MPJFTW5FW
--      | identity='anonymous' | role='owner' | status='active'
--      | joined_at = 2026-08-07 10:14:20+00
--
-- AI-183 avait purge la meme faute sous un autre nom (`system`) le 2026-08-04 et
-- s'etait ferme. La faute est revenue quatre jours plus tard sous le nom
-- `anonymous` : l'instance avait ete traitee, pas la classe. C'est le motif pour
-- lequel la regle descend ici plutot que dans un enieme appelant.
--
-- POURQUOI C'EST DEVENU DANGEREUX CE JOUR-LA. `anonymous` n'est pas un nom parmi
-- d'autres : c'est la sentinelle que `core.db.install_access_context` refuse
-- explicitement quand l'authentification est active, et celle qu'il laisse passer
-- SANS armer le plancher en self-host auth desactivee. Une ligne `org_members`
-- portant cette identite, `role='owner'`, `status='active'`, rend donc
-- `app.epic36_has_resource_access()` vrai pour un acteur que personne n'est.
-- Tant que le role applicatif portait BYPASSRLS la question ne se posait pas --
-- rien ne lisait la politique. Depuis la bascule, elle se pose.
--
-- CE QUE LA MIGRATION NE FAIT PAS. Elle ne touche pas au carve-out self-host :
-- ce chemin retourne AVANT d'armer quoi que ce soit et n'a jamais eu besoin d'une
-- ligne d'appartenance. Les six ecrivains de `app.org_members`
-- (`organizations_api`, `org_members_api`, `invitations`, `hosted_entry_scope`,
-- `self_hosted_instance_claim`) ecrivent tous un `person_...` reel : verifie
-- ligne a ligne le 2026-08-17. Aucun appelant legitime ne perd quoi que ce soit.
--
-- LA PURGE EST GARDEE. Une organisation dont le SEUL owner actif serait une
-- sentinelle deviendrait inatteignable si on retirait la ligne. Le DO ci-dessous
-- LEVE dans ce cas au lieu de detruire -- « ne pas detruire » passe avant
-- « corriger la classe ». En production le 2026-08-17 la question ne se posait
-- pas : l'org gardait deux owners humains actifs, la ligne a ete retiree, et
-- cette migration est donc idempotente sur cette base.

BEGIN;

DO $$
DECLARE
    orphelines text[];
BEGIN
    SELECT array_agg(org_id) INTO orphelines
    FROM (
        SELECT org_id FROM app.org_members
        WHERE identity IN ('anonymous', 'system') OR btrim(identity) = ''
        EXCEPT
        SELECT org_id FROM app.org_members
        WHERE role = 'owner' AND status = 'active'
          AND identity NOT IN ('anonymous', 'system') AND btrim(identity) <> ''
    ) AS sans_owner_humain;

    IF orphelines IS NOT NULL THEN
        RAISE EXCEPTION
            'org(s) % n''ont pour seul owner actif qu''une sentinelle. '
            'Retirer la ligne les rendrait inatteignables : inscrire un owner '
            'humain (POST /api/orgs/{id}/members) avant de rejouer cette migration.',
            orphelines;
    END IF;

    DELETE FROM app.org_members
    WHERE identity IN ('anonymous', 'system') OR btrim(identity) = '';
END
$$;

ALTER TABLE app.org_members
    DROP CONSTRAINT IF EXISTS org_members_identity_is_a_person;

ALTER TABLE app.org_members
    ADD CONSTRAINT org_members_identity_is_a_person
    CHECK (
        btrim(identity) <> ''
        AND identity NOT IN ('anonymous', 'system')
    );

COMMENT ON CONSTRAINT org_members_identity_is_a_person ON app.org_members IS
    'AI-183 : le registre d''appartenance ne contient que des personnes. '
    '`anonymous` et `system` sont des sentinelles du seam d''acces ; une ligne '
    'active a leur nom rend app.epic36_has_resource_access() vrai pour un acteur '
    'que personne n''est, donc ouvre le plancher RLS a qui presente la sentinelle. '
    'Une identite blanche-mais-non-vide passe un test de verite et ne correspond '
    'a personne : btrim() la refuse aussi.';

COMMIT;
