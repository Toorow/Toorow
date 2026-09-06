-- 315 -- the measurement grain: the object that says a measure is reported
--        against these dimensions, and that existed nowhere.
--
-- MEASURED before a line, 2026-08-27: `grep -rln "mdm_metric_dimension"` over
-- `server/` and `infra/` returns 0. The MDM models a metric and a dimension as
-- INDEPENDENT canonical fields (migration 032, `concept_kind IN ('metric',
-- 'dimension')`) and NO column relates one to another. Its only relational
-- object -- the common key (migration 258) -- is dimensions-only and refuses a
-- metric by name (`component_is_metric`). So there was nowhere to say the one
-- thing a report is made of: `spend` is reported by day, campaign, publisher,
-- format. A metric could be sliced only by whatever a physical mapping happened
-- to place beside it, never by governed dimensions.
--
-- THIS IS THE FOURTH FACT, and the MIRROR of the common key on the other axis.
-- Governance ratified it in `docs/product-architecture/governance.md`
-- § "Amendment, 2026-08-27 -- the fourth fact":
--
--   * these canonical fields are ONE business identity  -> common key (258)
--   * this physical column IS that canonical field      -> mapping (032)
--   * the cross goes this way, at this cardinality       -> relationship (142)
--   * this measure is reported against these dimensions  -> HERE
--
-- WHAT IT ADDS, AND WHAT IT REFUSES TO BE.
--
-- It binds ONE canonical measure (the head) to a SET of canonical dimensions
-- (the members) it is reported against. The head MUST be a metric and each
-- member a dimension -- the exact refusal of the common key, reflected: that key
-- requires dimensions and refuses a metric; this requires a metric head and
-- refuses a dimension one. The concept-kind is enforced by the code before any
-- write (`metric_dimensions.resolve_grain`), reading the same registry the
-- common key and every binding read.
--
-- A grain of ZERO dimensions is LEGAL -- a measure reported at no breakdown is a
-- total -- where a common key of zero components is not. That is the one and only
-- divergence, and it lives in the members CHECK below (`BETWEEN 0 AND 16`).
--
-- It is not a second vocabulary. The head and members are
-- `app.mdm_canonical_fields.id` -- nothing is recopied, and an archived, foreign
-- or metric member is refused by the code before the write, where the validation
-- already reads the registry (`canonical_field_registry`).
--
-- It stores no coverage. Which Datastreams implement the head or a member is
-- DERIVED from the published mapping versions at read time (story 71.2); a stored
-- figure would be wrong the next time a Datastream publishes, and silently.
--
-- THE HEAD IS MUTABLE, THE VERSIONS ARE NOT. `mdm_metric_dimensions` carries the
-- name, the description, the status and the current-version pointer. The head
-- measure and the members live in `mdm_metric_dimension_versions`, append-only --
-- with the RGPD hatch in the trigger's WHEN clause, the doctrine of migrations
-- 200 and 209: an append-only table that forgets it re-blocks an organization
-- erasure, and this repository has already repaired that twice.

BEGIN;

CREATE TABLE IF NOT EXISTS app.mdm_metric_dimensions (
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

    CONSTRAINT pk_mdm_metric_dimensions PRIMARY KEY (id),
    -- Same house-style id as the rest: ULID Crockford base32 behind a prefix
    -- that says what the object is.
    CONSTRAINT ck_mdm_metric_dimensions_id CHECK (id ~ '^mmd_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT ck_mdm_metric_dimensions_name CHECK (length(btrim(name)) BETWEEN 1 AND 120),
    CONSTRAINT uq_mdm_metric_dimensions_scope UNIQUE (id, org_id, project_id),
    -- The couple, never project_id alone: it stops a grain from carrying another
    -- org's id and crossing the RLS by its own column.
    CONSTRAINT fk_mdm_metric_dimensions_project
        FOREIGN KEY (org_id, project_id) REFERENCES app.projects (org_id, id)
        ON DELETE CASCADE
);

-- Two active grains of the same project do not share a name: the name is what a
-- human reads. An archived grain frees its own.
CREATE UNIQUE INDEX IF NOT EXISTS uq_mdm_metric_dimensions_active_name
    ON app.mdm_metric_dimensions (project_id, lower(btrim(name)))
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS app.mdm_metric_dimension_versions (
    id                   TEXT        NOT NULL,
    metric_dimensions_id TEXT        NOT NULL,
    org_id               TEXT        NOT NULL,
    project_id           TEXT        NOT NULL,
    version_number       INTEGER     NOT NULL CHECK (version_number >= 1),
    -- The head measure: an object {canonical_field_id, canonical_name,
    -- value_type}. The id is the identity the hash pins; the name is carried for
    -- the read, exactly as a member dict carries its own.
    head                 JSONB       NOT NULL
                         CHECK (jsonb_typeof(head) = 'object'
                                AND head->>'canonical_field_id' ~ '^mdm_[0-9A-HJKMNP-TV-Z]{26}$'),
    -- ORDERED array of {ordinal, canonical_field_id, canonical_name, value_type}.
    -- ZERO is legal -- a measure reported at no breakdown is a total -- and that
    -- is the ONE place this object diverges from the common key, whose components
    -- CHECK is `BETWEEN 1 AND 8`. Order is meaning: adding a dimension is a new
    -- version, and the hash says so.
    members              JSONB       NOT NULL
                         CHECK (jsonb_typeof(members) = 'array'
                                AND jsonb_array_length(members) BETWEEN 0 AND 16),
    content_hash         TEXT        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    created_by           TEXT        NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_mdm_metric_dimension_versions PRIMARY KEY (id),
    CONSTRAINT ck_mdm_metric_dimension_versions_id
        CHECK (id ~ '^mmdv_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_mdm_metric_dimension_versions_number
        UNIQUE (metric_dimensions_id, version_number),
    CONSTRAINT uq_mdm_metric_dimension_versions_scope UNIQUE (id, org_id, project_id),
    -- No uniqueness on (metric_dimensions_id, content_hash). What the product
    -- refuses is "your new version is identical to the CURRENT one" -- a named
    -- refusal, rendered by the domain. A constraint over the whole history would
    -- also forbid a deliberate return to an earlier composition, and would refuse
    -- it by a constraint name instead of a sentence.
    CONSTRAINT fk_mdm_metric_dimension_versions_grain
        FOREIGN KEY (metric_dimensions_id, org_id, project_id)
        REFERENCES app.mdm_metric_dimensions (id, org_id, project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_mdm_metric_dimension_versions_grain
    ON app.mdm_metric_dimension_versions (project_id, metric_dimensions_id, version_number DESC);

-- The head pointer can only name a version of THIS grain, in THIS project.
-- Without the triple, a crossed pointer would read another grain's members under
-- this one's name.
--
-- The UNIQUE index comes BEFORE the constraint: a foreign key needs a unique
-- index on the referenced columns, and the reverse order is 42830
-- InvalidForeignKey.
CREATE UNIQUE INDEX IF NOT EXISTS uq_mdm_metric_dimension_versions_owner_scope
    ON app.mdm_metric_dimension_versions (id, metric_dimensions_id, project_id);

ALTER TABLE app.mdm_metric_dimensions
    DROP CONSTRAINT IF EXISTS fk_mdm_metric_dimensions_current_version;
ALTER TABLE app.mdm_metric_dimensions
    ADD CONSTRAINT fk_mdm_metric_dimensions_current_version
    FOREIGN KEY (current_version_id, id, project_id)
    REFERENCES app.mdm_metric_dimension_versions (id, metric_dimensions_id, project_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE OR REPLACE FUNCTION app.reject_mdm_metric_dimension_version_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'mdm metric dimension versions are immutable'
        USING ERRCODE = '23000';
END;
$$;

DO $$
DECLARE
    target TEXT;
    guarded TEXT[] := ARRAY['mdm_metric_dimensions', 'mdm_metric_dimension_versions'];
BEGIN
    -- Append-only holds for the VERSIONS only; the head carries a status and a
    -- pointer that move. The two tables share the RLS.
    EXECUTE 'DROP TRIGGER IF EXISTS trg_mdm_metric_dimension_versions_immutable '
            'ON app.mdm_metric_dimension_versions';
    EXECUTE 'CREATE TRIGGER trg_mdm_metric_dimension_versions_immutable '
            'BEFORE UPDATE OR DELETE ON app.mdm_metric_dimension_versions '
            'FOR EACH ROW WHEN (current_setting(''app.rgpd_erasure'', true) '
            'IS DISTINCT FROM ''on'') EXECUTE FUNCTION '
            'app.reject_mdm_metric_dimension_version_mutation()';
    EXECUTE 'DROP TRIGGER IF EXISTS trg_mdm_metric_dimension_versions_block_truncate '
            'ON app.mdm_metric_dimension_versions';
    EXECUTE 'CREATE TRIGGER trg_mdm_metric_dimension_versions_block_truncate '
            'BEFORE TRUNCATE ON app.mdm_metric_dimension_versions FOR EACH STATEMENT '
            'EXECUTE FUNCTION app.reject_mdm_metric_dimension_version_mutation()';

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

GRANT SELECT, INSERT, UPDATE ON app.mdm_metric_dimensions TO connector;
GRANT SELECT, INSERT ON app.mdm_metric_dimension_versions TO connector;

COMMENT ON TABLE app.mdm_metric_dimensions IS
    'Story 71.1: the measurement grain -- one canonical measure (the head) '
    'reported against a set of canonical dimensions (the members). The mirror of '
    'the common key on the measure axis; governance.md amendment 2026-08-27.';

COMMIT;
