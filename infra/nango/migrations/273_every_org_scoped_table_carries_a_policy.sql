-- 273 -- Les 67 tables org-scopees qui n'avaient aucune politique en recoivent une (AI-299).
--
-- CE QUI A ETE MESURE, 2026-08-17, sur la base de production :
--
--   tables `app` portant `org_id` : 137
--   dont RLS activee ET forcee    :  66
--   dont AUCUNE politique         :  71
--
-- Et le mecanisme qui a produit ce chiffre, qui compte plus que le chiffre :
-- les 66 ont ete armees A LA MAIN, une ou deux a la fois, reparties sur 25
-- migrations. Aucune boucle, aucun registre, rien qui parcoure
-- `information_schema`. Personne n'a jamais eu la liste sous les yeux d'un seul
-- coup, donc chaque table neuve portant `org_id` sortait du perimetre en
-- silence. C'est pour ca que cette migration s'accompagne d'un cliquet
-- (`server/tests/core/test_rls_covers_every_org_scoped_table_pg.py`) : sans lui
-- l'ecart se reformerait a la table suivante.
--
-- QUATRE PREDICATS, ET AUCUN N'EST UN CHOIX DE STYLE. La forme reelle des
-- tables les impose, elle a ete lue et non supposee :
--
--   32 tables  project_id NOT NULL
--              -> epic36_has_resource_access(org_id, 'project', project_id)
--              C'est mot pour mot la forme des 63 politiques existantes.
--
--   17 tables  project_id NULLABLE, et deux en portent vraiment
--              (`dimension_labels` 3/3, `metric_semantics_audit` 3/9)
--              -> project_id NON NULL : comme ci-dessus
--                 project_id NULL     : ligne de portee ORG, donc appartenance
--              Sans cette seconde branche, une ligne de portee org disparaitrait
--              pour tout membre non-owner : `has_resource_access(org, 'project',
--              NULL)` ne peut matcher aucun `resource_grants.scope_id`.
--
--    1 table   datastream_id -> scope 'flux', forme des 11 existantes
--
--   11 tables  org_id seul -> appartenance active a l'org
--
--    7 tables  reference produit -> appartenance OU org_id = 'platform'
--              `app.organizations` porte DEUX lignes : `Toorow` (le locataire)
--              et `platform`, qui n'a AUCUN membre par construction et detient
--              la donnee de reference livree avec le produit (6 lignes de
--              `mdm_business_domains` et leurs 6 versions, mesurees). Armer ces
--              tables sur la seule appartenance les rendrait invisibles A TOUT
--              LE MONDE -- une regression franche, deguisee en durcissement.
--
-- POURQUOI UNE NOUVELLE FONCTION. `epic36_has_resource_access` exige un
-- (scope_type, scope_id), et `app.resource_grants` n'accepte que 'project' et
-- 'flux' (CHECK, verifie) : il n'existe AUCUN scope org dans le modele de
-- droits. Une table de portee org n'a donc rien a lui passer. `epic36_is_org_member`
-- pose la seule question qui a un sens la : cette identite est-elle membre actif
-- de cette org. Elle est SECURITY DEFINER avec `search_path` fige, comme sa
-- soeur, pour rester utilisable sans donner a `connector` un acces direct.
--
-- TROIS TABLES RESTENT DEHORS, chacune parce que l'armer fermerait le chemin qui
-- cree l'appartenance elle-meme. Le cliquet porte la meme liste et la meme
-- raison ; y ajouter une table exige d'ecrire pourquoi.
--
--   invitations                     une invitation se lit AVANT d'etre membre.
--                                   C'est sa definition.
--   instance_claims                 revendication self-hosted : precede toute
--                                   appartenance.
--   hosted_entry_scope_consumptions CREE l'org et son premier membre, donc
--                                   s'execute quand personne n'est membre.
--
-- CE QUE CETTE MIGRATION NE CHANGE PAS. Le predicat reste conditionne par
-- `toorow.enforce_epic36`. Les chemins de fond (`background_connection` --
-- scheduler, queue) ne l'arment pas et lisent donc exactement les memes lignes
-- qu'avant. La logique metier d'`access.py` reste la premiere barriere ; ceci
-- est la seconde, et elle etait absente sur ces tables, pas cassee.

BEGIN;

CREATE OR REPLACE FUNCTION app.epic36_is_org_member(target_org text)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO 'app', 'pg_temp'
AS $function$
    SELECT EXISTS (
        SELECT 1
        FROM app.org_members m
        WHERE m.org_id = target_org
          AND m.identity = app.epic36_identity()
          AND m.status = 'active'
    )
$function$;

COMMENT ON FUNCTION app.epic36_is_org_member(text) IS
    'AI-299 : la question de portee ORG. epic36_has_resource_access exige un '
    '(scope_type, scope_id) et resource_grants n''accepte que project/flux, donc '
    'une table de portee org n''a rien a lui passer. SECURITY DEFINER comme sa '
    'soeur : la politique reste evaluable sans donner a connector un acces direct '
    'a org_members.';

ALTER TABLE app.capability_exceptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.capability_exceptions FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS capability_exceptions_epic36 ON app.capability_exceptions;
CREATE POLICY capability_exceptions_epic36 ON app.capability_exceptions
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

ALTER TABLE app.cleanup_rule_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.cleanup_rule_versions FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS cleanup_rule_versions_epic36 ON app.cleanup_rule_versions;
CREATE POLICY cleanup_rule_versions_epic36 ON app.cleanup_rule_versions
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

ALTER TABLE app.cleanup_rules ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.cleanup_rules FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS cleanup_rules_epic36 ON app.cleanup_rules;
CREATE POLICY cleanup_rules_epic36 ON app.cleanup_rules
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

ALTER TABLE app.connector_activations ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.connector_activations FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS connector_activations_epic36 ON app.connector_activations;
CREATE POLICY connector_activations_epic36 ON app.connector_activations
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_is_org_member(org_id));

ALTER TABLE app.context_path_resolutions ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.context_path_resolutions FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS context_path_resolutions_epic36 ON app.context_path_resolutions;
CREATE POLICY context_path_resolutions_epic36 ON app.context_path_resolutions
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

ALTER TABLE app.context_review_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.context_review_requests FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS context_review_requests_epic36 ON app.context_review_requests;
CREATE POLICY context_review_requests_epic36 ON app.context_review_requests
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR (project_id IS NOT NULL AND app.epic36_has_resource_access(org_id, 'project', project_id))
           OR (project_id IS NULL AND app.epic36_is_org_member(org_id)));

ALTER TABLE app.control_cases ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.control_cases FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS control_cases_epic36 ON app.control_cases;
CREATE POLICY control_cases_epic36 ON app.control_cases
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

ALTER TABLE app.dataset_access_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.dataset_access_grants FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS dataset_access_grants_epic36 ON app.dataset_access_grants;
CREATE POLICY dataset_access_grants_epic36 ON app.dataset_access_grants
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_is_org_member(org_id));

ALTER TABLE app.datastream_capability_proposals ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.datastream_capability_proposals FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS datastream_capability_proposals_epic36 ON app.datastream_capability_proposals;
CREATE POLICY datastream_capability_proposals_epic36 ON app.datastream_capability_proposals
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

ALTER TABLE app.datastream_change_preparations ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.datastream_change_preparations FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS datastream_change_preparations_epic36 ON app.datastream_change_preparations;
CREATE POLICY datastream_change_preparations_epic36 ON app.datastream_change_preparations
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

-- `app.datastream_derived_columns` N'EST PAS ARMEE ICI, ET CE N'EST PAS UN OUBLI.
-- Elle existe en production (0 ligne) uniquement parce que la migration 263 --
-- « a second expression path is removed », posee par une session voisine et
-- encore en attente sur cette base -- la SUPPRIME. La generatrice de ce fichier
-- a lu la production, ou la table est encore la ; le depot, lui, l'a deja
-- retiree. Lui poser une politique produirait une migration qui echoue sur toute
-- installation neuve. Trouve en appliquant 271 sur une base jetable : la seule
-- divergence entre les 311 tables `app` de la production et les 310 du depot.

ALTER TABLE app.datastream_entity_binding_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.datastream_entity_binding_versions FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS datastream_entity_binding_versions_epic36 ON app.datastream_entity_binding_versions;
CREATE POLICY datastream_entity_binding_versions_epic36 ON app.datastream_entity_binding_versions
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

ALTER TABLE app.datastream_execution_phase_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.datastream_execution_phase_evidence FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS datastream_execution_phase_evidence_epic36 ON app.datastream_execution_phase_evidence;
CREATE POLICY datastream_execution_phase_evidence_epic36 ON app.datastream_execution_phase_evidence
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

ALTER TABLE app.datastream_execution_stage_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.datastream_execution_stage_evidence FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS datastream_execution_stage_evidence_epic36 ON app.datastream_execution_stage_evidence;
CREATE POLICY datastream_execution_stage_evidence_epic36 ON app.datastream_execution_stage_evidence
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

ALTER TABLE app.datastream_execution_step_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.datastream_execution_step_evidence FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS datastream_execution_step_evidence_epic36 ON app.datastream_execution_step_evidence;
CREATE POLICY datastream_execution_step_evidence_epic36 ON app.datastream_execution_step_evidence
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

ALTER TABLE app.datastream_output_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.datastream_output_versions FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS datastream_output_versions_epic36 ON app.datastream_output_versions;
CREATE POLICY datastream_output_versions_epic36 ON app.datastream_output_versions
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

ALTER TABLE app.datastream_outputs ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.datastream_outputs FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS datastream_outputs_epic36 ON app.datastream_outputs;
CREATE POLICY datastream_outputs_epic36 ON app.datastream_outputs
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

ALTER TABLE app.datastream_rollback_preparations ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.datastream_rollback_preparations FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS datastream_rollback_preparations_epic36 ON app.datastream_rollback_preparations;
CREATE POLICY datastream_rollback_preparations_epic36 ON app.datastream_rollback_preparations
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

ALTER TABLE app.datastream_setup_templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.datastream_setup_templates FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS datastream_setup_templates_epic36 ON app.datastream_setup_templates;
CREATE POLICY datastream_setup_templates_epic36 ON app.datastream_setup_templates
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_is_org_member(org_id) OR org_id = 'platform');

ALTER TABLE app.dimension_field_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.dimension_field_bindings FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS dimension_field_bindings_epic36 ON app.dimension_field_bindings;
CREATE POLICY dimension_field_bindings_epic36 ON app.dimension_field_bindings
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR (project_id IS NOT NULL AND app.epic36_has_resource_access(org_id, 'project', project_id))
           OR (project_id IS NULL AND app.epic36_is_org_member(org_id)));

ALTER TABLE app.dimension_labels ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.dimension_labels FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS dimension_labels_epic36 ON app.dimension_labels;
CREATE POLICY dimension_labels_epic36 ON app.dimension_labels
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR (project_id IS NOT NULL AND app.epic36_has_resource_access(org_id, 'project', project_id))
           OR (project_id IS NULL AND app.epic36_is_org_member(org_id)));

ALTER TABLE app.dimension_value_mappings ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.dimension_value_mappings FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS dimension_value_mappings_epic36 ON app.dimension_value_mappings;
CREATE POLICY dimension_value_mappings_epic36 ON app.dimension_value_mappings
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR (project_id IS NOT NULL AND app.epic36_has_resource_access(org_id, 'project', project_id))
           OR (project_id IS NULL AND app.epic36_is_org_member(org_id)));

ALTER TABLE app.dq_monitors ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.dq_monitors FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS dq_monitors_epic36 ON app.dq_monitors;
CREATE POLICY dq_monitors_epic36 ON app.dq_monitors
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

ALTER TABLE app.entity_match_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.entity_match_decisions FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS entity_match_decisions_epic36 ON app.entity_match_decisions;
CREATE POLICY entity_match_decisions_epic36 ON app.entity_match_decisions
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR (project_id IS NOT NULL AND app.epic36_has_resource_access(org_id, 'project', project_id))
           OR (project_id IS NULL AND app.epic36_is_org_member(org_id)));

ALTER TABLE app.entity_match_policy_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.entity_match_policy_versions FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS entity_match_policy_versions_epic36 ON app.entity_match_policy_versions;
CREATE POLICY entity_match_policy_versions_epic36 ON app.entity_match_policy_versions
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_is_org_member(org_id));

ALTER TABLE app.entity_observation_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.entity_observation_versions FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS entity_observation_versions_epic36 ON app.entity_observation_versions;
CREATE POLICY entity_observation_versions_epic36 ON app.entity_observation_versions
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

ALTER TABLE app.entity_source_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.entity_source_bindings FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS entity_source_bindings_epic36 ON app.entity_source_bindings;
CREATE POLICY entity_source_bindings_epic36 ON app.entity_source_bindings
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_is_org_member(org_id));

ALTER TABLE app.entity_source_identity_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.entity_source_identity_versions FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS entity_source_identity_versions_epic36 ON app.entity_source_identity_versions;
CREATE POLICY entity_source_identity_versions_epic36 ON app.entity_source_identity_versions
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_is_org_member(org_id));

ALTER TABLE app.event_configurations ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.event_configurations FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS event_configurations_epic36 ON app.event_configurations;
CREATE POLICY event_configurations_epic36 ON app.event_configurations
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

ALTER TABLE app.fx_rate_sets ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.fx_rate_sets FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS fx_rate_sets_epic36 ON app.fx_rate_sets;
CREATE POLICY fx_rate_sets_epic36 ON app.fx_rate_sets
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

ALTER TABLE app.governance_rule_sets ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.governance_rule_sets FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS governance_rule_sets_epic36 ON app.governance_rule_sets;
CREATE POLICY governance_rule_sets_epic36 ON app.governance_rule_sets
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

ALTER TABLE app.host_preflights ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.host_preflights FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS host_preflights_epic36 ON app.host_preflights;
CREATE POLICY host_preflights_epic36 ON app.host_preflights
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR (project_id IS NOT NULL AND app.epic36_has_resource_access(org_id, 'project', project_id))
           OR (project_id IS NULL AND app.epic36_is_org_member(org_id)));

ALTER TABLE app.inbound_brand_match_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.inbound_brand_match_decisions FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS inbound_brand_match_decisions_epic36 ON app.inbound_brand_match_decisions;
CREATE POLICY inbound_brand_match_decisions_epic36 ON app.inbound_brand_match_decisions
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

ALTER TABLE app.mapping_proposals ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.mapping_proposals FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS mapping_proposals_epic36 ON app.mapping_proposals;
CREATE POLICY mapping_proposals_epic36 ON app.mapping_proposals
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

ALTER TABLE app.master_data_aliases ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.master_data_aliases FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS master_data_aliases_epic36 ON app.master_data_aliases;
CREATE POLICY master_data_aliases_epic36 ON app.master_data_aliases
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR (project_id IS NOT NULL AND app.epic36_has_resource_access(org_id, 'project', project_id))
           OR (project_id IS NULL AND app.epic36_is_org_member(org_id)));

ALTER TABLE app.master_data_nodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.master_data_nodes FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS master_data_nodes_epic36 ON app.master_data_nodes;
CREATE POLICY master_data_nodes_epic36 ON app.master_data_nodes
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR (project_id IS NOT NULL AND app.epic36_has_resource_access(org_id, 'project', project_id))
           OR (project_id IS NULL AND app.epic36_is_org_member(org_id)));

ALTER TABLE app.master_data_object_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.master_data_object_versions FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS master_data_object_versions_epic36 ON app.master_data_object_versions;
CREATE POLICY master_data_object_versions_epic36 ON app.master_data_object_versions
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR (project_id IS NOT NULL AND app.epic36_has_resource_access(org_id, 'project', project_id))
           OR (project_id IS NULL AND app.epic36_is_org_member(org_id)));

ALTER TABLE app.master_data_project_associations ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.master_data_project_associations FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS master_data_project_associations_epic36 ON app.master_data_project_associations;
CREATE POLICY master_data_project_associations_epic36 ON app.master_data_project_associations
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

ALTER TABLE app.master_data_registries ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.master_data_registries FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS master_data_registries_epic36 ON app.master_data_registries;
CREATE POLICY master_data_registries_epic36 ON app.master_data_registries
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR (project_id IS NOT NULL AND app.epic36_has_resource_access(org_id, 'project', project_id))
           OR (project_id IS NULL AND app.epic36_is_org_member(org_id)));

ALTER TABLE app.master_data_source_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.master_data_source_bindings FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS master_data_source_bindings_epic36 ON app.master_data_source_bindings;
CREATE POLICY master_data_source_bindings_epic36 ON app.master_data_source_bindings
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

ALTER TABLE app.master_data_type_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.master_data_type_versions FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS master_data_type_versions_epic36 ON app.master_data_type_versions;
CREATE POLICY master_data_type_versions_epic36 ON app.master_data_type_versions
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_is_org_member(org_id) OR org_id = 'platform');

ALTER TABLE app.mcp_capability_contexts ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.mcp_capability_contexts FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS mcp_capability_contexts_epic36 ON app.mcp_capability_contexts;
CREATE POLICY mcp_capability_contexts_epic36 ON app.mcp_capability_contexts
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_is_org_member(org_id));

ALTER TABLE app.mdm_business_classification_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.mdm_business_classification_versions FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS mdm_business_classification_versions_epic36 ON app.mdm_business_classification_versions;
CREATE POLICY mdm_business_classification_versions_epic36 ON app.mdm_business_classification_versions
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_is_org_member(org_id) OR org_id = 'platform');

ALTER TABLE app.mdm_business_classifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.mdm_business_classifications FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS mdm_business_classifications_epic36 ON app.mdm_business_classifications;
CREATE POLICY mdm_business_classifications_epic36 ON app.mdm_business_classifications
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_is_org_member(org_id) OR org_id = 'platform');

ALTER TABLE app.mdm_business_domain_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.mdm_business_domain_versions FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS mdm_business_domain_versions_epic36 ON app.mdm_business_domain_versions;
CREATE POLICY mdm_business_domain_versions_epic36 ON app.mdm_business_domain_versions
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_is_org_member(org_id) OR org_id = 'platform');

ALTER TABLE app.mdm_business_domains ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.mdm_business_domains FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS mdm_business_domains_epic36 ON app.mdm_business_domains;
CREATE POLICY mdm_business_domains_epic36 ON app.mdm_business_domains
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_is_org_member(org_id) OR org_id = 'platform');

ALTER TABLE app.mdm_business_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.mdm_business_links FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS mdm_business_links_epic36 ON app.mdm_business_links;
CREATE POLICY mdm_business_links_epic36 ON app.mdm_business_links
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

ALTER TABLE app.metric_definitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.metric_definitions FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS metric_definitions_epic36 ON app.metric_definitions;
CREATE POLICY metric_definitions_epic36 ON app.metric_definitions
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR (project_id IS NOT NULL AND app.epic36_has_resource_access(org_id, 'project', project_id))
           OR (project_id IS NULL AND app.epic36_is_org_member(org_id)));

ALTER TABLE app.metric_semantics_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.metric_semantics_audit FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS metric_semantics_audit_epic36 ON app.metric_semantics_audit;
CREATE POLICY metric_semantics_audit_epic36 ON app.metric_semantics_audit
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR (project_id IS NOT NULL AND app.epic36_has_resource_access(org_id, 'project', project_id))
           OR (project_id IS NULL AND app.epic36_is_org_member(org_id)));

ALTER TABLE app.operation_preparations ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.operation_preparations FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS operation_preparations_epic36 ON app.operation_preparations;
CREATE POLICY operation_preparations_epic36 ON app.operation_preparations
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

ALTER TABLE app.org_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.org_members FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS org_members_epic36 ON app.org_members;
CREATE POLICY org_members_epic36 ON app.org_members
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_is_org_member(org_id));

ALTER TABLE app.org_plan ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.org_plan FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS org_plan_epic36 ON app.org_plan;
CREATE POLICY org_plan_epic36 ON app.org_plan
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_is_org_member(org_id));

ALTER TABLE app.org_plan_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.org_plan_history FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS org_plan_history_epic36 ON app.org_plan_history;
CREATE POLICY org_plan_history_epic36 ON app.org_plan_history
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_is_org_member(org_id));

ALTER TABLE app.overlap_groups ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.overlap_groups FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS overlap_groups_epic36 ON app.overlap_groups;
CREATE POLICY overlap_groups_epic36 ON app.overlap_groups
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR (project_id IS NOT NULL AND app.epic36_has_resource_access(org_id, 'project', project_id))
           OR (project_id IS NULL AND app.epic36_is_org_member(org_id)));

ALTER TABLE app.project_access_handoffs ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.project_access_handoffs FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS project_access_handoffs_epic36 ON app.project_access_handoffs;
CREATE POLICY project_access_handoffs_epic36 ON app.project_access_handoffs
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

ALTER TABLE app.project_flux ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.project_flux FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS project_flux_epic36 ON app.project_flux;
CREATE POLICY project_flux_epic36 ON app.project_flux
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

ALTER TABLE app.project_grant_changes ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.project_grant_changes FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS project_grant_changes_epic36 ON app.project_grant_changes;
CREATE POLICY project_grant_changes_epic36 ON app.project_grant_changes
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

ALTER TABLE app.publication_confirmations ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.publication_confirmations FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS publication_confirmations_epic36 ON app.publication_confirmations;
CREATE POLICY publication_confirmations_epic36 ON app.publication_confirmations
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

ALTER TABLE app.reference_tables ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.reference_tables FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS reference_tables_epic36 ON app.reference_tables;
CREATE POLICY reference_tables_epic36 ON app.reference_tables
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

ALTER TABLE app.resource_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.resource_grants FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS resource_grants_epic36 ON app.resource_grants;
CREATE POLICY resource_grants_epic36 ON app.resource_grants
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_is_org_member(org_id));

ALTER TABLE app.setup_journeys ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.setup_journeys FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS setup_journeys_epic36 ON app.setup_journeys;
CREATE POLICY setup_journeys_epic36 ON app.setup_journeys
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR (project_id IS NOT NULL AND app.epic36_has_resource_access(org_id, 'project', project_id))
           OR (project_id IS NULL AND app.epic36_is_org_member(org_id)));

ALTER TABLE app.source_metric_mappings ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.source_metric_mappings FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS source_metric_mappings_epic36 ON app.source_metric_mappings;
CREATE POLICY source_metric_mappings_epic36 ON app.source_metric_mappings
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR (project_id IS NOT NULL AND app.epic36_has_resource_access(org_id, 'project', project_id))
           OR (project_id IS NULL AND app.epic36_is_org_member(org_id)));

ALTER TABLE app.tax_fee_preset_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.tax_fee_preset_versions FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tax_fee_preset_versions_epic36 ON app.tax_fee_preset_versions;
CREATE POLICY tax_fee_preset_versions_epic36 ON app.tax_fee_preset_versions
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_is_org_member(org_id) OR org_id = 'platform');

ALTER TABLE app.tracked_entities ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.tracked_entities FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tracked_entities_epic36 ON app.tracked_entities;
CREATE POLICY tracked_entities_epic36 ON app.tracked_entities
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_is_org_member(org_id));

ALTER TABLE app.tracked_entity_alerts ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.tracked_entity_alerts FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tracked_entity_alerts_epic36 ON app.tracked_entity_alerts;
CREATE POLICY tracked_entity_alerts_epic36 ON app.tracked_entity_alerts
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

ALTER TABLE app.value_mapping_assignments ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.value_mapping_assignments FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS value_mapping_assignments_epic36 ON app.value_mapping_assignments;
CREATE POLICY value_mapping_assignments_epic36 ON app.value_mapping_assignments
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'flux', datastream_id));

ALTER TABLE app.value_mapping_table_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.value_mapping_table_versions FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS value_mapping_table_versions_epic36 ON app.value_mapping_table_versions;
CREATE POLICY value_mapping_table_versions_epic36 ON app.value_mapping_table_versions
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR (project_id IS NOT NULL AND app.epic36_has_resource_access(org_id, 'project', project_id))
           OR (project_id IS NULL AND app.epic36_is_org_member(org_id)));

ALTER TABLE app.value_mapping_tables ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.value_mapping_tables FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS value_mapping_tables_epic36 ON app.value_mapping_tables;
CREATE POLICY value_mapping_tables_epic36 ON app.value_mapping_tables
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR (project_id IS NOT NULL AND app.epic36_has_resource_access(org_id, 'project', project_id))
           OR (project_id IS NULL AND app.epic36_is_org_member(org_id)));

COMMIT;
