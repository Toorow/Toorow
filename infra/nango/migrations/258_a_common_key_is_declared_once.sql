-- 258 -- la cle commune : l'objet qui dit que deux Datastreams parlent du meme
--        identifiant metier, et qui n'existait nulle part
--
-- MESURE le 2026-08-13, avant d'ecrire une ligne : `grep -rn "common_key"` sur
-- `server/`, `infra/nango/migrations/` et `ui/admin/src/` rend DEUX occurrences,
-- toutes deux une variable locale de `ai_paths.py:673`. Croiser deux sources
-- demande trois faits, et le depot n'en portait que deux :
--
--   * ce champ physique EST ce champ canonique
--     -> `mapping_payload.fields[].binding.mdm_target` (migration 032)
--   * le croisement va dans ce sens, a cette cardinalite, avec ce fan-out
--     -> `app.semantic_view_version_relationships` (migration 142)
--   * ces champs canoniques sont UNE MEME IDENTITE METIER
--     -> personne.
--
-- La consequence se lit dans la table 142 : elle stocke `from_columns` et
-- `to_columns` et AUCUNE reference a une identite partagee. Deux relations qui
-- expriment la meme cle metier -- `Day + Campaign` entre depense et conversions,
-- puis entre depense et revenus -- ne peuvent pas etre reconnues comme la meme
-- cle. Chaque relation redeclarait la sienne, et rien ne pouvait les comparer.
--
-- CE QUE CETTE MIGRATION AJOUTE, ET CE QU'ELLE REFUSE D'ETRE.
--
-- Elle ajoute une IDENTITE, pas une jointure. Une cle commune dit « ces sources
-- parlent du meme jour et de la meme campagne ». Elle ne dit pas « une ligne de
-- l'une rencontre une ligne de l'autre » : cette seconde phrase appartient a la
-- relation, et seule une relation qui epingle une VERSION exacte de cle est
-- executable. C'est pourquoi la colonne ajoutee en bas pointe la VERSION et
-- jamais la cle.
--
-- Elle n'est pas un second vocabulaire canonique. Les composants sont des
-- `app.mdm_canonical_fields.id` -- rien n'est recopie, et un composant archive
-- ou d'un autre projet est refuse par le code avant l'ecriture, la ou la
-- validation lit deja le registre (`canonical_field_registry`).
--
-- Elle ne stocke aucune couverture. Quels Datastreams implementent un composant
-- se DERIVE des versions de mapping publiees au moment de la lecture ; une
-- couverture stockee serait fausse des la publication suivante.
--
-- LA TETE EST MUTABLE, LES VERSIONS NE LE SONT PAS. `mdm_common_keys` porte le
-- nom, la description, le statut et le pointeur de version courante. Les
-- composants vivent dans `mdm_common_key_versions`, append-only -- avec
-- l'echappatoire RGPD dans la clause WHEN du trigger, doctrine des migrations
-- 200 et 209 : une table append-only qui l'oublie rebloque l'effacement d'une
-- organisation, et on l'a deja repare deux fois.

BEGIN;

CREATE TABLE IF NOT EXISTS app.mdm_common_keys (
    id                  TEXT        NOT NULL,
    org_id              TEXT        NOT NULL,
    project_id          TEXT        NOT NULL,
    name                TEXT        NOT NULL,
    description         TEXT,
    status              TEXT        NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'archived')),
    current_version_id  TEXT,
    created_by          TEXT        NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_mdm_common_keys PRIMARY KEY (id),
    -- Meme forme d'identifiant que le reste de la maison : ULID Crockford
    -- base32 derriere un prefixe qui dit ce que l'objet est.
    CONSTRAINT ck_mdm_common_keys_id CHECK (id ~ '^mck_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT ck_mdm_common_keys_name CHECK (length(btrim(name)) BETWEEN 1 AND 120),
    CONSTRAINT uq_mdm_common_keys_scope UNIQUE (id, org_id, project_id),
    -- Le couple, jamais le seul `project_id` : c'est ce qui empeche une cle de
    -- porter l'org d'une autre et de traverser la RLS par sa propre colonne.
    CONSTRAINT fk_mdm_common_keys_project
        FOREIGN KEY (org_id, project_id) REFERENCES app.projects (org_id, id)
        ON DELETE CASCADE
);

-- Deux cles actives du meme projet ne portent pas le meme nom : le nom est ce
-- qu'un humain lit dans une suggestion de croisement. Une cle archivee libere
-- le sien.
CREATE UNIQUE INDEX IF NOT EXISTS uq_mdm_common_keys_active_name
    ON app.mdm_common_keys (project_id, lower(btrim(name)))
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS app.mdm_common_key_versions (
    id                  TEXT        NOT NULL,
    common_key_id       TEXT        NOT NULL,
    org_id              TEXT        NOT NULL,
    project_id          TEXT        NOT NULL,
    version_number      INTEGER     NOT NULL CHECK (version_number >= 1),
    -- Tableau ORDONNE d'objets {ordinal, canonical_field_id, canonical_name,
    -- value_type}. L'ordre fait partie de l'identite : [Day, Campaign] et
    -- [Campaign, Day] sont deux cles, et leur hash le dit.
    components          JSONB       NOT NULL
                        CHECK (jsonb_typeof(components) = 'array'
                               AND jsonb_array_length(components) BETWEEN 1 AND 8),
    content_hash        TEXT        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    created_by          TEXT        NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_mdm_common_key_versions PRIMARY KEY (id),
    CONSTRAINT ck_mdm_common_key_versions_id
        CHECK (id ~ '^mckv_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_mdm_common_key_versions_number UNIQUE (common_key_id, version_number),
    CONSTRAINT uq_mdm_common_key_versions_scope UNIQUE (id, org_id, project_id),
    -- PAS d'unicite (common_key_id, content_hash). Ce que le produit refuse est
    -- « ta nouvelle version est identique a la version COURANTE » -- refus nomme,
    -- rendu par le domaine. Une contrainte sur toute l'histoire interdirait en
    -- plus le retour deliberé a une composition anterieure, et le refuserait par
    -- un nom de contrainte au lieu d'une phrase.
    CONSTRAINT fk_mdm_common_key_versions_key
        FOREIGN KEY (common_key_id, org_id, project_id)
        REFERENCES app.mdm_common_keys (id, org_id, project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_mdm_common_key_versions_key
    ON app.mdm_common_key_versions (project_id, common_key_id, version_number DESC);

-- Le pointeur de tete ne peut designer qu'une version de CETTE cle, dans CE
-- projet. Sans le triplet, un pointeur croise aurait fait lire les composants
-- d'une autre cle sous le nom de celle-ci.
--
-- L'index UNIQUE vient AVANT la contrainte : une cle etrangere exige un index
-- unique sur les colonnes referencees, et l'ordre inverse rend 42830
-- InvalidForeignKey -- mesure du 2026-08-13, premiere execution de ce fichier.
CREATE UNIQUE INDEX IF NOT EXISTS uq_mdm_common_key_versions_owner_scope
    ON app.mdm_common_key_versions (id, common_key_id, project_id);

ALTER TABLE app.mdm_common_keys
    DROP CONSTRAINT IF EXISTS fk_mdm_common_keys_current_version;
ALTER TABLE app.mdm_common_keys
    ADD CONSTRAINT fk_mdm_common_keys_current_version
    FOREIGN KEY (current_version_id, id, project_id)
    REFERENCES app.mdm_common_key_versions (id, common_key_id, project_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE OR REPLACE FUNCTION app.reject_mdm_common_key_version_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'mdm common key versions are immutable'
        USING ERRCODE = '23000';
END;
$$;

DO $$
DECLARE
    target TEXT;
    guarded TEXT[] := ARRAY['mdm_common_keys', 'mdm_common_key_versions'];
BEGIN
    -- L'append-only ne vaut que pour les VERSIONS ; la tete porte un statut et
    -- un pointeur qui bougent. Les deux tables partagent en revanche la RLS.
    EXECUTE 'DROP TRIGGER IF EXISTS trg_mdm_common_key_versions_immutable '
            'ON app.mdm_common_key_versions';
    EXECUTE 'CREATE TRIGGER trg_mdm_common_key_versions_immutable '
            'BEFORE UPDATE OR DELETE ON app.mdm_common_key_versions '
            'FOR EACH ROW WHEN (current_setting(''app.rgpd_erasure'', true) '
            'IS DISTINCT FROM ''on'') EXECUTE FUNCTION '
            'app.reject_mdm_common_key_version_mutation()';
    EXECUTE 'DROP TRIGGER IF EXISTS trg_mdm_common_key_versions_block_truncate '
            'ON app.mdm_common_key_versions';
    EXECUTE 'CREATE TRIGGER trg_mdm_common_key_versions_block_truncate '
            'BEFORE TRUNCATE ON app.mdm_common_key_versions FOR EACH STATEMENT '
            'EXECUTE FUNCTION app.reject_mdm_common_key_version_mutation()';

    FOREACH target IN ARRAY guarded LOOP
        EXECUTE format('ALTER TABLE app.%I ENABLE ROW LEVEL SECURITY', target);
        EXECUTE format('ALTER TABLE app.%I FORCE ROW LEVEL SECURITY', target);
        EXECUTE format('DROP POLICY IF EXISTS %I ON app.%I', target || '_strict', target);
        EXECUTE format(
            'CREATE POLICY %I ON app.%I USING ('
            'current_setting(''toorow.enforce_epic36'', true) IS DISTINCT FROM ''on'' '
            'OR app.epic36_has_resource_access(org_id, ''project'', project_id)) '
            'WITH CHECK ('
            'current_setting(''toorow.enforce_epic36'', true) IS DISTINCT FROM ''on'' '
            'OR app.epic36_has_resource_access(org_id, ''project'', project_id))',
            target || '_strict', target
        );
    END LOOP;
END $$;

GRANT SELECT, INSERT, UPDATE ON app.mdm_common_keys TO connector;
GRANT SELECT, INSERT ON app.mdm_common_key_versions TO connector;

-- LA RELATION EPINGLE UNE VERSION DE CLE, ET C'EST TOUT CE QUI CHANGE ICI.
-- Colonne NULLABLE, ajoutee sans defaut : toutes les versions de vue existantes
-- se relisent octet pour octet, et une relation qui n'epingle rien reste ce
-- qu'elle etait -- une jointure declaree a la main, non rattachee a une identite
-- partagee, donc non proposable comme croisement gouverne.
ALTER TABLE app.semantic_view_version_relationships
    ADD COLUMN IF NOT EXISTS mdm_common_key_version_id TEXT;

ALTER TABLE app.semantic_view_version_relationships
    DROP CONSTRAINT IF EXISTS fk_semantic_view_relationship_common_key;
ALTER TABLE app.semantic_view_version_relationships
    ADD CONSTRAINT fk_semantic_view_relationship_common_key
    FOREIGN KEY (mdm_common_key_version_id)
    REFERENCES app.mdm_common_key_versions (id) ON DELETE RESTRICT;

CREATE INDEX IF NOT EXISTS idx_semantic_view_relationships_common_key
    ON app.semantic_view_version_relationships (mdm_common_key_version_id)
    WHERE mdm_common_key_version_id IS NOT NULL;

COMMENT ON COLUMN app.semantic_view_version_relationships.mdm_common_key_version_id IS
    'La version exacte de cle commune que cette relation rend executable. NULL = '
    'relation declaree sans identite partagee : elle joint, mais aucune suggestion '
    'de croisement ne peut la nommer.';

COMMIT;
