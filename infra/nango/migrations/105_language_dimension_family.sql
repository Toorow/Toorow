-- 105_language_dimension_family.sql
--
-- Story 27.8 -- « langue » n'est pas une dimension, c'en est trois.
--
-- Le pays est UN concept a plusieurs graphies : 'FR', 'France', 'FRANCE' designent
-- la meme chose et les conformer est SANS PERTE. La langue est l'inverse :
-- PLUSIEURS CONCEPTS SOUS UN MOT. Nos propres catalogues le prouvent -- un match
-- textuel sur 'language' ramene quatre concepts et deux metriques homonymes.
--
-- Trois dimensions conformees DISTINCTES, jamais fusionnees :
--   * audience_language  -- OBSERVE chez la personne (reglage navigateur/appareil)
--   * content_language   -- propriete de l'ASSET servi (la creation)
--   * targeting_language -- INTENTION declaree (le reglage de diffusion)
--
-- Elles ne sont JAMAIS sommees ni comparees entre elles : la garde est de meme
-- nature qu'un KEEP_SEPARATE (server/core/language_dimensions.py reutilise
-- litteralement les constantes de core.metric_reconciliation). L'ecart
-- targeting <-> audience est une INFORMATION, pas une anomalie a reconcilier.
--
-- Cette migration ne cree AUCUNE table de valeurs : le niveau VALEUR reste
-- app.dimension_value_mappings (052) et les trois dimensions y vivent comme
-- n'importe quelle autre dimension conformee. Elle apporte deux choses :
--
--   1. un nouveau suggestion_method 'standard_rollup' sur 052, pour que le
--      pre-mapping RECOMMANDE 'fr-FR' -> 'fr' soit PRE-REMPLI et VISIBLE avec sa
--      provenance exacte (le standard BCP 47), distinct d'un 'normalized'. Un
--      defaut est legitime ICI parce qu'il est DERIVE DU STANDARD, pas d'une
--      opinion metier -- difference avec les marches geographiques (epic-37) ou
--      aucun defaut n'est legitime. Il reste SURCHARGEABLE par la cascade
--      existante : une ligne CONFIRMED en ORG/PROJET bat le defaut PLATFORM.
--      INTERDIT et refuse en code : conformer 'fr-BE' vers 'fr-FR', decision
--      metier deguisee en normalisation.
--
--   2. app.dimension_field_bindings -- le niveau SCHEMA du binding
--      (connecteur, rapport, champ source) -> dimension canonique, avec CONFIANCE
--      et PREUVE CITEE (la description du fournisseur, verbatim). C'est la piece
--      qui manquait : 052 porte le niveau VALEUR et connait le connecteur, pas le
--      rapport ni le champ ; les manifests portent le niveau SCHEMA en statique.
--      La lignee inversee de 27.9 clé ses lignes exactement sur ce grain
--      (connector, report_id, source_field).
--
-- INVARIANT (identique a 052) : RIEN N'EST JAMAIS AUTO-CONFIRME. Le chemin
-- automatique n'ecrit que 'proposed' / 'pending' / 'excluded' ; 'confirmed' et
-- 'rejected' ne sont atteignables que par un acte humain. Un match exact n'est
-- qu'une CONFIANCE plus haute, jamais un blanc-seing. 'pending' est le statut de
-- l'ambiguite assumee : quand la description du fournisseur ne dit pas si le
-- champ est une observation ou un ciblage, la proposition RESTE pending -- elle
-- peut porter une proposition, jamais une decision.
--
-- SCOPING : triplet (scope_level, org_id, project_id) repete a l'identique de
-- 049/052/103. Les FK ON DELETE CASCADE font que la disparition d'une org / d'un
-- projet emporte ses bindings specifiques ; les defauts PLATFORM survivent. La
-- table est MUTABLE (un binding se corrige, se confirme, se rejette) : aucun
-- garde append-only n'est pose dessus, donc RIEN a ajouter a l'allowlist RGPD de
-- la migration 099 -- l'effacement d'org passe par la FK, decouverte par le
-- graphe de core/org_purge.py.
--
-- AUDIT : pas de nouvelle table d'audit. Le registre append-only
-- app.metric_semantics_audit (049) a une colonne entity_type TEXT libre ; 27.8 y
-- ecrit avec entity_type='dimension_field_binding' via le meme
-- _write_semantics_audit. Dependance SOUPLE sur 049.
--
-- ID prefixe (prefixed-ULID) : 'dfb_'.
--
-- AD-2 : aucun nom de fournisseur ici. `connector`, `report_id` et `source_field`
-- sont OPAQUES (des donnees), exactement comme la colonne `connector` de 052.
--
-- VOCABULAIRE DE VALEURS : les valeurs sont du BCP 47 ('fr', 'fr-FR') -- langue en
-- minuscules, region en majuscules. Le demi-registre LANGUE est le seed gouverne
-- dbt/seeds/dim_language.csv ; le demi-registre REGION reste dim_country.csv (on
-- ne forke pas le registre pays). Pas d'ISO 639 seul : la locale serait perdue a
-- l'entree, definitivement.
--
-- PENDING -- NOT applied to Supabase (human gate), comme 052/103.
-- Apply with: psql "$SUPABASE_DB_URL" -f infra/nango/migrations/105_language_dimension_family.sql
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- ---------------------------------------------------------------------------
-- 1. 'standard_rollup' : la provenance « derive du standard » du pre-mapping.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF to_regclass('app.dimension_value_mappings') IS NOT NULL THEN
        ALTER TABLE app.dimension_value_mappings
            DROP CONSTRAINT IF EXISTS dimension_value_mappings_suggestion_method_check;
        ALTER TABLE app.dimension_value_mappings
            ADD CONSTRAINT dimension_value_mappings_suggestion_method_check
            CHECK (suggestion_method IS NULL
                   OR suggestion_method IN ('exact','normalized','similarity','manual',
                                            'standard_rollup'));
        EXECUTE $c$
            COMMENT ON COLUMN app.dimension_value_mappings.suggestion_method IS
                'exact|normalized|similarity|manual|standard_rollup. standard_rollup (27.8) '
                '= la proposition est DERIVEE DU STANDARD BCP 47 (fr-FR -> fr), pas d''une '
                'heuristique textuelle ni d''une opinion metier ; pre-remplie, visible comme '
                'telle, et surchargeable par une ligne confirmee plus specifique '
                '(PROJECT > ORG > PLATFORM).'
        $c$;
    END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- 2. app.dimension_field_bindings -- niveau SCHEMA, avec confiance et preuve.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.dimension_field_bindings (
    id                   TEXT        PRIMARY KEY,          -- prefixed ULID: 'dfb_'
    connector            TEXT        NOT NULL,             -- opaque (AD-2)
    report_id            TEXT        NOT NULL DEFAULT 'unknown',  -- grain 27.9 ; 'unknown' = non dit, jamais invente
    source_field         TEXT        NOT NULL,             -- champ tel que le fournisseur le nomme
    canonical_dimension  TEXT,                             -- NULL tant qu'aucune dimension n'est proposee
    status               TEXT        NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('proposed','pending','confirmed','rejected','excluded')),
    -- CONFIANCE : [0,1], jamais 1.0 par le chemin automatique. Une confiance n'est
    -- pas une permission -- seule une confirmation humaine rend un binding resolvable.
    confidence           DOUBLE PRECISION NOT NULL DEFAULT 0
                         CHECK (confidence >= 0 AND confidence <= 1),
    -- PREUVE CITEE : la description du fournisseur, VERBATIM (jamais paraphrasee --
    -- une paraphrase serait notre opinion, or tout l'interet de la preuve est
    -- qu'elle n'est PAS de nous).
    evidence_quote       TEXT        NOT NULL DEFAULT '',
    evidence_source      TEXT        NOT NULL DEFAULT 'no_evidence'
                         CHECK (evidence_source IN ('catalog_field_description',
                                                    'catalog_field_name','no_evidence','human')),
    evidence_markers     TEXT        NOT NULL DEFAULT '',  -- marqueurs generiques reellement apparies (pipe-delimite)
    rationale            TEXT        NOT NULL DEFAULT '',  -- le raisonnement, lisible, auditable
    pending_reason       TEXT,                             -- pourquoi la preuve ne tranche pas
    excluded_reason      TEXT,                             -- pourquoi le champ est hors famille (metrique homonyme, attribut catalogue)
    -- scope triplet (identique a 049/052/103)
    scope_level          TEXT        NOT NULL CHECK (scope_level IN ('PLATFORM','ORG','PROJECT')),
    org_id               TEXT        REFERENCES app.organizations(id) ON DELETE CASCADE,
    project_id           TEXT        REFERENCES app.projects(id)      ON DELETE CASCADE,
    created_by           TEXT        NOT NULL,             -- 'system' pour une proposition automatique
    reviewed_by          TEXT,                             -- qui a confirme/rejete (NULL sinon)
    reviewed_at          TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_dimension_field_bindings_scope_cols CHECK (
        (scope_level = 'PLATFORM' AND org_id IS NULL     AND project_id IS NULL)
     OR (scope_level = 'ORG'      AND org_id IS NOT NULL AND project_id IS NULL)
     OR (scope_level = 'PROJECT'  AND project_id IS NOT NULL)
    ),
    -- Un binding CONFIRME designe forcement une dimension : c'est la seule chose
    -- qu'une confirmation puisse vouloir dire.
    CONSTRAINT ck_dimension_field_bindings_confirmed_has_dimension CHECK (
        status <> 'confirmed' OR canonical_dimension IS NOT NULL
    ),
    -- Un champ EXCLU n'est rattache a rien : l'exclusion est un verdict de
    -- non-appartenance a la famille, pas un rattachement discret.
    CONSTRAINT ck_dimension_field_bindings_excluded_has_no_dimension CHECK (
        status <> 'excluded' OR canonical_dimension IS NULL
    ),
    CONSTRAINT ck_dimension_field_bindings_excluded_reason CHECK (
        (status = 'excluded') = (excluded_reason IS NOT NULL)
    )
);

-- UNICITE : un binding par (scope, connecteur, rapport, champ source). Le grain
-- RAPPORT est le point de la story 27.9 : un meme champ existe sur plusieurs types
-- de rapports, et seul le plan tire sait lequel a reellement ete pris. Le
-- COALESCE(...,'') est OBLIGATOIRE (NULL <> NULL laisserait passer deux lignes).
CREATE UNIQUE INDEX IF NOT EXISTS uq_dimension_field_bindings_scope_key
    ON app.dimension_field_bindings
    (scope_level, COALESCE(org_id, ''), COALESCE(project_id, ''),
     connector, report_id, source_field);

-- Chemin de resolution (resolve_field_bindings) : confirmes uniquement.
CREATE INDEX IF NOT EXISTS ix_dimension_field_bindings_resolve
    ON app.dimension_field_bindings (connector, report_id, source_field, status);

-- « qu'est-ce qui alimente cette dimension ? » (vue inverse 27.9) et file de curation.
CREATE INDEX IF NOT EXISTS ix_dimension_field_bindings_dimension
    ON app.dimension_field_bindings (canonical_dimension, status);

COMMENT ON TABLE app.dimension_field_bindings IS
    'Story 27.8 -- binding niveau SCHEMA (connecteur, rapport, champ source) -> dimension '
    'canonique, avec CONFIANCE et PREUVE CITEE. Statuts : proposed (proposition defendable) '
    '| pending (la preuve ne tranche pas -- peut porter une proposition, jamais une '
    'decision) | confirmed (acte humain, SEUL statut resolu) | rejected (faux ami consigne) '
    '| excluded (hors famille : metrique homonyme, attribut catalogue). Rien n''est jamais '
    'auto-confirme.';

-- ---------------------------------------------------------------------------
-- 3. Libelles PLATFORM par defaut des trois dimensions (103), si la table existe.
--    Le libelle appartient au client : ce sont des DEFAUTS PLATFORM, surchargeables
--    par ORG puis par PROJET. L'identifiant, lui, ne bouge jamais.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF to_regclass('app.dimension_labels') IS NOT NULL THEN
        INSERT INTO app.dimension_labels
            (id, canonical_dimension, display_label, description, scope_level, org_id,
             project_id, created_by, created_at, updated_at)
        VALUES
            ('dlb_27_8_audience_language', 'audience_language', 'Audience language',
             'Language observed on the person reached (browser / device / user setting).',
             'PLATFORM', NULL, NULL, 'system', now(), now()),
            ('dlb_27_8_content_language', 'content_language', 'Content language',
             'Language of the asset actually served (the creative / the content).',
             'PLATFORM', NULL, NULL, 'system', now(), now()),
            ('dlb_27_8_targeting_language', 'targeting_language', 'Targeting language',
             'Language declared as targeted (a delivery setting, not an observation).',
             'PLATFORM', NULL, NULL, 'system', now(), now())
        ON CONFLICT (scope_level, COALESCE(org_id, ''), COALESCE(project_id, ''),
                     canonical_dimension) DO NOTHING;
    END IF;
END
$$;

COMMIT;
