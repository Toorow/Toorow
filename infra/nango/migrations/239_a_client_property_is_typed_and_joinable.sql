-- Story 64.2 (AI-232): a client property is typed, and joinable in the warehouse.
--
-- THE POINT THAT DECIDES WHETHER ANY OF THIS IS USEFUL. Story 64.13 established
-- that `version_scope='node'` gives every identity its own version and its own
-- `payload` JSONB -- "a matching key and a JSON of its properties", in the owner's
-- words. Two things were still missing, and without either the properties exist in
-- Postgres and are INVISIBLE to analysis:
--
--   1. nothing says what the payload may contain, so `duration_s` is a number on
--      Monday and a string on Tuesday and no reader can bucket it;
--   2. the mirror is scalars-only (Story 64.9, the fee/tax precedent), so a JSONB
--      blob cannot ride it at all.
--
-- WHY THE CONTRACT LIVES ON THE REGISTRY. It is a property of the KIND -- every
-- video has a duration -- and the registry is the kind's single owner. Country and
-- the other registries default to an empty array and are untouched.
--
-- WHY THE VALUE TYPES ARE THE SEMANTIC LAYER'S. `integer, decimal, money, ratio,
-- percent, duration, string, date, timestamp, boolean` is verbatim
-- `semantic_expressions.VALUE_TYPES`. An attribute declared here can therefore
-- become a Concept without translation, which is the whole road to `age_days`
-- (Story 64.11). A second vocabulary would have needed a mapping between two lists
-- meaning the same thing -- the defect this epic has already removed twice.
--
-- WHY EAV AND NOT A COLUMN PER ATTRIBUTE. The attributes are the CLIENT's, unknown
-- at build time. A column per attribute would need a migration per client, which
-- is the opposite of a generic object model. One row per (node, attribute) is
-- relational, scalars-only, and needs no knowledge of the kinds.
--
-- WHAT THE VIEW REFUSES TO DO. It does not guess a type. A value is cast ONLY to
-- what the contract declares, and a cast that fails sets `type_mismatch` instead
-- of yielding NULL: a property that silently became NULL is a number nobody can
-- question, and finding it later on a chart is finding it too late.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. The contract, on the kind's owner.
--
-- An array of {name, value_type, unit?, label?}. Validated in
-- `core.object_kind_attributes` rather than by a CHECK: the shape rules -- no
-- duplicate name, a known value_type, a unit only where it means something --
-- are the kind of refusal that must carry a sentence, and a CHECK carries a
-- constraint name.
-- ---------------------------------------------------------------------------
ALTER TABLE app.master_data_registries
    ADD COLUMN IF NOT EXISTS attribute_contract JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE app.master_data_registries
    DROP CONSTRAINT IF EXISTS ck_master_data_registries_attribute_contract;
ALTER TABLE app.master_data_registries
    ADD CONSTRAINT ck_master_data_registries_attribute_contract
    CHECK (jsonb_typeof(attribute_contract) = 'array');

COMMENT ON COLUMN app.master_data_registries.attribute_contract IS
    'Story 64.2: what a node of this kind may carry in payload.attributes -- [{name, value_type, unit?, label?}]. value_type is semantic_expressions.VALUE_TYPES verbatim, so an attribute can become a Concept without translation.';

-- ---------------------------------------------------------------------------
-- 2. The projection. One row per (node, attribute), scalars only.
--
-- Reads the CURRENT version of each node-scoped registry. A draft is not a fact:
-- properties become visible to analysis when they are published, exactly like the
-- hierarchy they sit beside.
--
-- `value_text` always carries the value as written, so an unresolved or
-- mistyped property is still readable by a human. The typed columns are populated
-- only where the contract asks for them AND the cast succeeds.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW app.master_data_node_attributes_dim_v AS
WITH declared AS (
    SELECT r.id            AS registry_id,
           r.project_id,
           r.org_id,
           c.value ->> 'name'        AS attribute,
           c.value ->> 'value_type'  AS value_type,
           c.value ->> 'unit'        AS unit
      FROM app.master_data_registries r
      CROSS JOIN LATERAL jsonb_array_elements(r.attribute_contract) AS c(value)
     WHERE r.version_scope = 'node'
),
carried AS (
    SELECT v.project_id,
           v.org_id,
           v.registry_id,
           v.node_id,
           v.id                      AS version_id,
           a.key                     AS attribute,
           a.value                   AS raw
      FROM app.master_data_object_versions v
      CROSS JOIN LATERAL jsonb_each(COALESCE(v.payload -> 'attributes', '{}'::jsonb)) AS a(key, value)
     WHERE v.status = 'current'
       AND v.node_id IS NOT NULL
)
SELECT c.org_id,
       c.project_id,
       c.registry_id,
       c.node_id,
       c.version_id,
       c.attribute,
       d.value_type,
       d.unit,
       -- Always readable, whatever the type says.
       CASE WHEN jsonb_typeof(c.raw) = 'string' THEN c.raw #>> '{}' ELSE c.raw::text END
                                                            AS value_text,
       CASE WHEN d.value_type IN ('integer','decimal','money','ratio','percent','duration')
             AND jsonb_typeof(c.raw) = 'number'
            THEN (c.raw #>> '{}')::NUMERIC END              AS value_number,
       CASE WHEN d.value_type IN ('date','timestamp')
             AND jsonb_typeof(c.raw) = 'string'
             AND (c.raw #>> '{}') ~ '^\d{4}-\d{2}-\d{2}'
            THEN ((c.raw #>> '{}')::DATE) END               AS value_date,
       CASE WHEN d.value_type = 'boolean' AND jsonb_typeof(c.raw) = 'boolean'
            THEN (c.raw)::TEXT::BOOLEAN END                 AS value_bool,
       -- The honest flag. An attribute the contract never declared, or a value the
       -- declared type cannot hold: visible as a row with a reason, never a silent
       -- NULL and never a dropped row.
       (d.attribute IS NULL)                                AS undeclared,
       (d.value_type IS NOT NULL AND (
            (d.value_type IN ('integer','decimal','money','ratio','percent','duration')
             AND jsonb_typeof(c.raw) <> 'number')
         OR (d.value_type IN ('date','timestamp') AND jsonb_typeof(c.raw) <> 'string')
         OR (d.value_type = 'boolean' AND jsonb_typeof(c.raw) <> 'boolean')
         OR (d.value_type = 'string' AND jsonb_typeof(c.raw) <> 'string')
       ))                                                   AS type_mismatch
  FROM carried c
  LEFT JOIN declared d
    ON d.registry_id = c.registry_id
   AND d.attribute   = c.attribute;

COMMENT ON VIEW app.master_data_node_attributes_dim_v IS
    'Story 64.2: one row per (node, attribute) of the CURRENT node-scoped versions, scalars only, for the warehouse mirror. Never guesses a type: a value is cast only to what the contract declares, and a failed cast sets type_mismatch rather than yielding a NULL nobody can question.';

COMMIT;
