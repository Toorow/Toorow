-- Story 64.14 (AI-248): a canonical field says WHICH OBJECT it qualifies.
--
-- ARBITRATION, Jean 2026-08-08: "d'un cote tu vas avoir une video que tu vas
-- venir mapper avec son ID [...] la duree peut etre definie dans le MDM comme
-- DUREE DE LA VIDEO. ou description ou transcript."
--
-- WHAT WAS WRONG, AND I SHIPPED BOTH HALVES OF IT THE SAME DAY. One fact was
-- declared in two places that each knew half of it:
--
--   mdm_canonical_fields   name, concept_kind, unit, aggregation   -- NO value_type
--   attribute_contract     name, value_type, unit                  -- NO kind, NO aggregation
--   semantic_concept_versions                                      -- the union, governed
--
-- `semantic_concept_versions.value_type` is NOT NULL, so a canonical field could
-- never become a Concept: its half was missing. The attribute contract could not
-- either: its own half was missing. Two declarations, neither publishable, and
-- nothing keeping them equal -- the exact defect epic 64 removed four times and
-- that I recommitted while closing it.
--
-- WHAT THIS MIGRATION DOES. The definition becomes ONE row. A canonical field
-- carries its value_type, and it may say which object kind it qualifies. The
-- attributes of an object stop being a second list and become a QUERY: the
-- canonical fields that name it.
--
-- WHY `object_kind` IS NULLABLE. `date`, `country`, `clicks` qualify no object --
-- they are the platform's own vocabulary, and forcing them to name one would
-- invent an owner. A field with no object kind is a field about the fact itself.
--
-- WHY THE UNIQUENESS MOVES. `uq_mdm_canonical_name_project` made a name unique
-- per project, so one project could not carry the duration OF A VIDEO and the
-- duration OF A PODCAST. Under the arbitration those are two definitions, so the
-- name becomes unique per (project, object kind). The platform index is
-- untouched: a platform field never names an object.
--
-- THE TABLE IS EMPTY (measured 2026-08-08: 0 rows at both scopes), which is why
-- `value_type` lands NOT NULL with no default and no backfill.

BEGIN;

ALTER TABLE app.mdm_canonical_fields
    ADD COLUMN IF NOT EXISTS value_type TEXT NOT NULL
        CHECK (value_type IN ('integer', 'decimal', 'money', 'ratio', 'percent',
                              'duration', 'string', 'date', 'timestamp', 'boolean'));

ALTER TABLE app.mdm_canonical_fields
    ADD COLUMN IF NOT EXISTS object_kind TEXT
        CHECK (object_kind IS NULL OR object_kind ~ '^[a-z][a-z0-9_]{1,39}$');

-- A field that qualifies an object belongs to a project: object registries are
-- project-scoped, so a platform field naming one would point at nothing.
ALTER TABLE app.mdm_canonical_fields
    DROP CONSTRAINT IF EXISTS ck_mdm_canonical_fields_object_scope;
ALTER TABLE app.mdm_canonical_fields
    ADD CONSTRAINT ck_mdm_canonical_fields_object_scope
    CHECK (object_kind IS NULL OR project_id IS NOT NULL);

DROP INDEX IF EXISTS app.uq_mdm_canonical_name_project;

CREATE UNIQUE INDEX IF NOT EXISTS uq_mdm_canonical_name_project_object
    ON app.mdm_canonical_fields (project_id, COALESCE(object_kind, ''), canonical_name)
    WHERE project_id IS NOT NULL AND status <> 'archived';

COMMENT ON COLUMN app.mdm_canonical_fields.value_type IS
    'Story 64.14: the half this table never carried. semantic_expressions.VALUE_TYPES verbatim -- semantic_concept_versions.value_type is NOT NULL, so without it a canonical field could never become a Concept.';

COMMENT ON COLUMN app.mdm_canonical_fields.object_kind IS
    'Story 64.14 (arbitration 2026-08-08): which object this field qualifies -- the duration OF THE VIDEO, not a free-floating duration. NULL = the platform vocabulary, about the fact itself and about no object.';

-- ---------------------------------------------------------------------------
-- The projection reads the DECLARATIONS, not a second list.
--
-- Migration 239 read `master_data_registries.attribute_contract`, a JSONB array
-- added the same morning. Under the arbitration the attributes of an object ARE
-- the canonical fields that name it, so the contract has nothing left to hold.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW app.master_data_node_attributes_dim_v AS
WITH declared AS (
    SELECT r.id             AS registry_id,
           r.project_id,
           r.org_id,
           f.canonical_name AS attribute,
           f.value_type,
           f.unit
      FROM app.master_data_registries r
      JOIN app.mdm_canonical_fields f
        ON f.project_id = r.project_id
       AND f.object_kind = r.object_kind
       AND f.status = 'active'
     WHERE r.version_scope = 'node'
),
carried AS (
    SELECT v.project_id,
           v.org_id,
           v.registry_id,
           v.node_id,
           v.id   AS version_id,
           a.key  AS attribute,
           a.value AS raw
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

-- The second store goes, rather than staying as something nobody writes and
-- somebody eventually does. Added 2026-08-08, dropped 2026-08-08, 0 rows: the
-- shortest life a column can have, and shorter than the drift it would have
-- caused.
ALTER TABLE app.master_data_registries
    DROP CONSTRAINT IF EXISTS ck_master_data_registries_attribute_contract;
ALTER TABLE app.master_data_registries
    DROP COLUMN IF EXISTS attribute_contract;

COMMIT;
