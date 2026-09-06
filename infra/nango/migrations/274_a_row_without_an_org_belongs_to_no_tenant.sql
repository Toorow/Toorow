-- 274 -- Une ligne sans org n'appartient a aucun locataire, donc elle reste lisible (AI-299).
--
-- CE QUE LA MIGRATION 273 A CASSE, ET COMMENT JE L'AI SU. Elle armait 67 tables.
-- La verification qui suit -- « membre reel, plancher arme » sur la production --
-- a rendu ceci :
--
--   dimension_labels : etranger+arme=0   membre+arme=0   etranger+baisse=3
--
-- Le membre voyait ZERO. Ce n'est pas de l'isolation, c'est une disparition. La
-- verite terrain :
--
--   SELECT org_id, project_id, count(*) FROM app.dimension_labels GROUP BY 1,2;
--   -- -> (NULL, NULL, 3)
--
-- Ces trois libelles de dimension ne portent NI org NI projet : ce sont des
-- libelles plateforme, livres avec le produit. Le predicat de la 273 se lit
-- `project_id IS NULL AND epic36_is_org_member(org_id)`, et
-- `epic36_is_org_member(NULL)` compare `m.org_id = NULL` -- toujours NULL, donc
-- jamais vrai. La ligne devenait invisible pour tout le monde, y compris son
-- proprietaire legitime.
--
-- Mesure du sinistre avant reparation : 9 des 67 tables armees ont `org_id`
-- NULLABLE, et DEUX en portent reellement -- `dimension_labels` (3 lignes) et
-- `metric_semantics_audit` (6). Les sept autres etaient vides, donc la meme
-- faute y dormait sans se voir : elle serait apparue a la premiere insertion.
--
-- LA REGLE, ET ELLE EXISTAIT DEJA DANS LE DEPOT. `render_share_access_events`,
-- armee bien avant celle-ci, porte `OR (org_id IS NULL)` dans sa politique. Une
-- ligne sans org n'appartient a aucun locataire : il n'y a rien a isoler, et la
-- cacher n'protege personne. Je ne l'ai pas lue avant d'ecrire la 273 -- c'est
-- la seule raison pour laquelle ce fichier existe.
--
-- POURQUOI UNE MIGRATION DE PLUS ET PAS UNE CORRECTION DE LA 273. La 273 est
-- appliquee (production et bases jetables). Une migration appliquee ne se
-- re-edite jamais : son empreinte est au ledger, et la corriger sur place ferait
-- diverger silencieusement les bases qui l'ont deja passee.

BEGIN;

-- Les sept tables de portee projet dont `org_id` est NULLABLE. Le predicat est
-- reproduit ENTIER plutot que « modifie » : une politique se remplace, elle ne
-- se patche pas, et un lecteur doit voir la regle complete a un seul endroit.
DROP POLICY IF EXISTS dimension_field_bindings_epic36 ON app.dimension_field_bindings;
CREATE POLICY dimension_field_bindings_epic36 ON app.dimension_field_bindings
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR org_id IS NULL
           OR (project_id IS NOT NULL AND app.epic36_has_resource_access(org_id, 'project', project_id))
           OR (project_id IS NULL AND app.epic36_is_org_member(org_id)));

DROP POLICY IF EXISTS dimension_labels_epic36 ON app.dimension_labels;
CREATE POLICY dimension_labels_epic36 ON app.dimension_labels
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR org_id IS NULL
           OR (project_id IS NOT NULL AND app.epic36_has_resource_access(org_id, 'project', project_id))
           OR (project_id IS NULL AND app.epic36_is_org_member(org_id)));

DROP POLICY IF EXISTS dimension_value_mappings_epic36 ON app.dimension_value_mappings;
CREATE POLICY dimension_value_mappings_epic36 ON app.dimension_value_mappings
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR org_id IS NULL
           OR (project_id IS NOT NULL AND app.epic36_has_resource_access(org_id, 'project', project_id))
           OR (project_id IS NULL AND app.epic36_is_org_member(org_id)));

DROP POLICY IF EXISTS metric_definitions_epic36 ON app.metric_definitions;
CREATE POLICY metric_definitions_epic36 ON app.metric_definitions
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR org_id IS NULL
           OR (project_id IS NOT NULL AND app.epic36_has_resource_access(org_id, 'project', project_id))
           OR (project_id IS NULL AND app.epic36_is_org_member(org_id)));

DROP POLICY IF EXISTS metric_semantics_audit_epic36 ON app.metric_semantics_audit;
CREATE POLICY metric_semantics_audit_epic36 ON app.metric_semantics_audit
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR org_id IS NULL
           OR (project_id IS NOT NULL AND app.epic36_has_resource_access(org_id, 'project', project_id))
           OR (project_id IS NULL AND app.epic36_is_org_member(org_id)));

DROP POLICY IF EXISTS overlap_groups_epic36 ON app.overlap_groups;
CREATE POLICY overlap_groups_epic36 ON app.overlap_groups
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR org_id IS NULL
           OR (project_id IS NOT NULL AND app.epic36_has_resource_access(org_id, 'project', project_id))
           OR (project_id IS NULL AND app.epic36_is_org_member(org_id)));

DROP POLICY IF EXISTS source_metric_mappings_epic36 ON app.source_metric_mappings;
CREATE POLICY source_metric_mappings_epic36 ON app.source_metric_mappings
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR org_id IS NULL
           OR (project_id IS NOT NULL AND app.epic36_has_resource_access(org_id, 'project', project_id))
           OR (project_id IS NULL AND app.epic36_is_org_member(org_id)));

-- Les deux tables de reference produit dont `org_id` est NULLABLE. Elles avaient
-- deja la branche `org_id = 'platform'` ; il leur manquait le cas ou la ligne ne
-- nomme aucune org du tout, qui est la meme idee ecrite avec NULL.
DROP POLICY IF EXISTS master_data_type_versions_epic36 ON app.master_data_type_versions;
CREATE POLICY master_data_type_versions_epic36 ON app.master_data_type_versions
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR org_id IS NULL
           OR app.epic36_is_org_member(org_id)
           OR org_id = 'platform');

DROP POLICY IF EXISTS tax_fee_preset_versions_epic36 ON app.tax_fee_preset_versions;
CREATE POLICY tax_fee_preset_versions_epic36 ON app.tax_fee_preset_versions
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR org_id IS NULL
           OR app.epic36_is_org_member(org_id)
           OR org_id = 'platform');

COMMIT;
