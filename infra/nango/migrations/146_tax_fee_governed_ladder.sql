-- Story 48.4: Tax & Fees moves from a mutable table to the governed rule ladder.
--
-- The baseline this migration corrects, stated exactly:
--
--   * `app.fee_tax_rules` (migration 119) is a MUTABLE row set with a per-row
--     `status`. A rate could be edited in place after a Result had been computed
--     from it, and nothing anywhere recorded which rate produced which number.
--     "Published versions are immutable and carry complete evidence" (AC2) is not
--     expressible on that table, and no column could be added to make it so: the
--     defect is that the row IS the authority.
--   * `app.project_preferences.fee_tax_alignment_enabled` (migration 119) is a
--     SECOND activation authority beside `app.project_capabilities`. Two booleans
--     that mean the same thing always diverge eventually, and every dbt Tax model
--     reads the second one -- so a Project whose capability was disabled through
--     the Change Set kept producing Tax rows.
--   * No relation records who says a rate is true. `origin` names the CHANNEL that
--     created the row (`auto_country`, `operator`, `media_plan`, `llm`), never the
--     AUTHORITY behind it, so a statutory rate, a platform's invoice practice and a
--     client's own markup were the same kind of fact.
--   * `app.datastream_source_types` (migration 119) has no production writer. The
--     compiler read it anyway, so "this Datastream is PAID_MEDIA" was a claim
--     backed by an empty table.
--   * Nothing records what a PUBLICATION actually showed about tax: posture,
--     tax code, measured impressions, transaction counts. Applicability was
--     therefore compiled from a mapping, which states what a Datastream is
--     supposed to publish, not what it did.
--
-- WHY NO NEW RULE TABLE. Story 49.4 owns the Rule Set workbench, and migration 144
-- (with 145 completing its approvals) already created its generic shape -- `governance_rule_sets` /
-- `governance_rule_set_versions` / `governance_rule_set_exceptions`, family-typed
-- and opaque to the family's meaning. Money, FX ingestion and Timezone are mounted
-- on it. Tax & Fees is the fourth family and its ladder lives in the version's
-- `ordered_rules`. A `tax_fee_rules` table would be a fifth lifecycle for the same
-- object, which is the defect this story removes rather than repeats.
--
-- WHAT HAPPENS TO THE EXISTING ROWS. They are PRESERVED and DEMOTED, never deleted
-- and never auto-published. A legacy row genuinely lacks an authority, a pinned
-- jurisdiction version, a Rest of World posture and a source version -- that
-- absence IS the finding. Promoting it into a published ladder would fabricate the
-- evidence the audit asks for. `app.tax_fee_rule_migration_candidates` therefore
-- carries each row verbatim plus the typed list of what it is missing, for an
-- operator to review. `app.fee_tax_rules` itself is left in place: it stops being
-- authority the moment the flattening views stop reading it, and deleting it would
-- destroy the only inventory of what still has to be re-declared.
--
-- Additive and idempotent. No table is dropped and no row is deleted. No rate,
-- country or amount is INSERTed anywhere (E41-NFR04).

BEGIN;

-- ---------------------------------------------------------------------------
-- 0. The RGPD erasure hatch for migration 144's three immutable relations.
--
--    Not this story's tables -- 48.3's. Migration 098/099 established that an org
--    erasure runs with `app.rgpd_erasure = on` and that every protective DELETE
--    trigger on an org-scoped table must yield to it through an EXPLICIT allowlist.
--    143 had to do exactly this for 140's four tables; 144 then added three more
--    and joined no allowlist either, so an organization with one published Money
--    Policy, one FX rate batch or one published Timezone Policy can no longer be
--    erased. Nobody would learn that until a real erasure request arrived.
--
--    Fixed here because it is the same class, not because it is this story's code.
--    `fx_rate_observations` is included: its rows are org-scoped through the rate
--    set, and org_purge walks the FK graph to reach them.
-- ---------------------------------------------------------------------------
DO $migration$
DECLARE
    target_tables CONSTANT text[] := ARRAY[
        'governance_rule_set_versions',
        'fx_rate_set_versions',
        'fx_rate_observations'
    ];
    guard CONSTANT text :=
        ' WHEN (current_setting(''app.rgpd_erasure'', true) IS DISTINCT FROM ''on'') ';
    rec record;
    new_def text;
BEGIN
    FOR rec IN
        SELECT c.relname AS tbl, t.tgname, pg_get_triggerdef(t.oid) AS def
        FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'app'
          AND c.relname = ANY (target_tables)
          AND NOT t.tgisinternal
          AND (t.tgtype & 8) > 0                      -- fires on DELETE
    LOOP
        CONTINUE WHEN rec.def LIKE '%rgpd_erasure%';
        new_def := replace(rec.def, ' EXECUTE FUNCTION ', guard || ' EXECUTE FUNCTION ');
        IF new_def = rec.def THEN
            RAISE EXCEPTION 'could not add the erasure hatch to %.%', rec.tbl, rec.tgname;
        END IF;
        EXECUTE format('DROP TRIGGER %I ON app.%I', rec.tgname, rec.tbl);
        EXECUTE new_def;
    END LOOP;
END
$migration$;


-- ---------------------------------------------------------------------------
-- 1. app.tax_fee_preset_versions -- prequalified PROPOSAL input (AC3).
--
--    A preset is a versioned statement about the world, not a Project policy. It
--    is the input to a proposal an operator confirms, edits or rejects; nothing
--    executes from this table, and `fee_tax_country_resolution` never reads it.
--
--    `qualifications` is NOT decoration. AC3: "Country, source type and observed
--    invoice/source evidence narrow candidate rules. They do not by themselves
--    prove VAT, DST, withholding, reverse charge, payment fees or contractual
--    pass-through." A UK Digital Services Tax entry is a narrow tax on qualifying
--    group revenues with activity and threshold tests, so `GB + 2%` is a reference
--    candidate and the tests that make it applicable live in this column. A preset
--    whose qualifications are unproven compiles to an INERT proposal.
--
--    org_id NULL means platform-shared. Those rows belong to no tenant, which is
--    why this table is deliberately absent from the erasure allowlist above.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.tax_fee_preset_versions (
    id                          TEXT        PRIMARY KEY,
    org_id                      TEXT        REFERENCES app.organizations(id) ON DELETE CASCADE,
    preset_key                  TEXT        NOT NULL
                                    CHECK (preset_key ~ '^[a-z][a-z0-9_]{1,126}$'),
    version_number              INTEGER     NOT NULL CHECK (version_number > 0),
    status                      TEXT        NOT NULL DEFAULT 'draft'
                                    CHECK (status IN ('draft', 'published', 'superseded', 'withdrawn')),
    label                       TEXT        NOT NULL,

    -- Who says so, where it is written, and as of which edition of that source.
    issuer                      TEXT        NOT NULL,
    source_reference            TEXT        NOT NULL,
    source_reference_version    TEXT        NOT NULL,
    authoritative_url           TEXT,
    document_ref                TEXT,

    -- Where it applies. A CODE from the governed vocabulary, never a Project's
    -- own country id: a preset is shared, and a Project id inside it would leak
    -- one tenant's Master Data into another's proposal.
    jurisdiction_kind           TEXT        NOT NULL DEFAULT 'none'
                                    CHECK (jurisdiction_kind IN ('none', 'country', 'market', 'region')),
    jurisdiction_code           TEXT,

    -- What it taxes or charges, in the source's own terms. AC3 requires the rate
    -- TYPE and the supply qualifiers to survive: an EU standard VAT rate is not a
    -- rate on "everything", and dropping the subject is how it becomes one.
    taxable_subject             TEXT        NOT NULL,
    service_scope               TEXT,

    -- The ladder shape it proposes. Validated against the same routed pairs the
    -- ladder profile enforces, so a preset can never propose a rule no evaluator
    -- routes -- which would contribute exactly zero behind a complete flag.
    category                    TEXT        NOT NULL,
    form                        TEXT        NOT NULL,
    rate                        NUMERIC(12,6),
    amount_micros               BIGINT,
    cpm_micros                  BIGINT,
    currency                    TEXT        CHECK (currency IS NULL OR currency ~ '^[A-Z]{3}$'),
    base_target                 TEXT        NOT NULL,

    thresholds                  JSONB       NOT NULL DEFAULT '[]'::jsonb
                                    CHECK (jsonb_typeof(thresholds) = 'array'),
    qualifications              JSONB       NOT NULL DEFAULT '[]'::jsonb
                                    CHECK (jsonb_typeof(qualifications) = 'array'),
    assumptions                 JSONB       NOT NULL DEFAULT '[]'::jsonb
                                    CHECK (jsonb_typeof(assumptions) = 'array'),

    published_on                DATE,
    effective_from              DATE        NOT NULL,
    effective_to                DATE,
    last_verified_on            DATE,
    verification_status         TEXT        NOT NULL DEFAULT 'unverified'
                                    CHECK (verification_status IN ('unverified', 'verified', 'stale')),

    content_hash                TEXT        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    created_by                  TEXT        NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT ck_tax_fee_preset_interval
        CHECK (effective_to IS NULL OR effective_to >= effective_from),
    -- A jurisdiction-bearing preset must name where; a jurisdiction-free one must
    -- not pretend to. Silence in either direction is what makes a preset look
    -- applicable somewhere nobody chose.
    CONSTRAINT ck_tax_fee_preset_jurisdiction
        CHECK ((jurisdiction_kind = 'none' AND jurisdiction_code IS NULL)
            OR (jurisdiction_kind <> 'none' AND jurisdiction_code IS NOT NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_tax_fee_preset_versions
    ON app.tax_fee_preset_versions (COALESCE(org_id, ''), preset_key, version_number);

CREATE INDEX IF NOT EXISTS idx_tax_fee_preset_versions_lookup
    ON app.tax_fee_preset_versions (jurisdiction_kind, jurisdiction_code, status);

-- A published preset is frozen. A proposal cites its version; a version whose
-- content can change makes the citation meaningless.
CREATE OR REPLACE FUNCTION app.reject_published_tax_fee_preset_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status = 'published' THEN
            RAISE EXCEPTION 'published Tax & Fee preset versions are never deleted';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status = 'published' AND NEW.status NOT IN ('superseded', 'withdrawn') THEN
        RAISE EXCEPTION 'published Tax & Fee preset versions are immutable';
    END IF;
    IF OLD.status = 'published' AND NEW.content_hash IS DISTINCT FROM OLD.content_hash THEN
        RAISE EXCEPTION 'a published Tax & Fee preset version cannot change content';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_tax_fee_preset_versions_immutable ON app.tax_fee_preset_versions;
CREATE TRIGGER trg_tax_fee_preset_versions_immutable
    BEFORE UPDATE OR DELETE ON app.tax_fee_preset_versions
    FOR EACH ROW EXECUTE FUNCTION app.reject_published_tax_fee_preset_mutation();


-- ---------------------------------------------------------------------------
-- 2. app.tax_fee_rule_migration_candidates -- the legacy rows, preserved.
--
--    Every `app.fee_tax_rules` row, carried verbatim with the typed list of the
--    evidence it lacks. Reviewable, never executable: nothing joins this table to
--    a fact, and the flattening views below do not read it.
--
--    This is the inventory of re-declaration work. Deleting the legacy rows
--    instead would have destroyed the only record that they ever existed, and the
--    only list of what an operator still has to state.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.tax_fee_rule_migration_candidates (
    id                  TEXT        PRIMARY KEY,
    project_id          TEXT        NOT NULL REFERENCES app.projects(id) ON DELETE CASCADE,
    legacy_rule_id      TEXT        NOT NULL,
    legacy_status       TEXT        NOT NULL,
    legacy_payload      JSONB       NOT NULL CHECK (jsonb_typeof(legacy_payload) = 'object'),
    -- What a published ladder rule requires and this row cannot supply.
    missing_evidence    TEXT[]      NOT NULL DEFAULT '{}',
    review_state        TEXT        NOT NULL DEFAULT 'pending'
                            CHECK (review_state IN ('pending', 'adopted', 'rejected')),
    adopted_rule_key    TEXT,
    reviewed_by         TEXT,
    reviewed_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, legacy_rule_id)
);

CREATE INDEX IF NOT EXISTS idx_tax_fee_rule_migration_candidates_state
    ON app.tax_fee_rule_migration_candidates (project_id, review_state);

-- The one-time carry-over. Idempotent through ON CONFLICT DO NOTHING, and it
-- INSERTs no rate of its own: every value comes from a row that already existed.
INSERT INTO app.tax_fee_rule_migration_candidates
    (id, project_id, legacy_rule_id, legacy_status, legacy_payload, missing_evidence)
SELECT
    'tfmc_' || r.id,
    r.project_id,
    r.id,
    r.status,
    to_jsonb(r) - 'created_by',
    ARRAY_REMOVE(ARRAY[
        'authority_kind',
        'source_evidence',
        CASE WHEN r.conditions ? 'country' OR r.conditions ? 'market'
             THEN 'geography_hierarchy_version_id' END,
        CASE WHEN r.conditions ? 'country' OR r.conditions ? 'market'
             THEN 'rest_of_world_posture' END,
        CASE WHEN r.conditions ? 'country' OR r.conditions ? 'market'
             THEN 'unknown_posture' END,
        'money_basis'
    ], NULL)
FROM app.fee_tax_rules r
ON CONFLICT (project_id, legacy_rule_id) DO NOTHING;


-- ---------------------------------------------------------------------------
-- 3. app.datastream_tax_evidence -- what a PUBLICATION showed (AC4).
--
--    The tax half of what `datastream_money_evidence` does for currency, and it
--    exists for the same reason: a Connector contract and a mapping state what a
--    Datastream is SUPPOSED to publish; only a publication states what it DID.
--
--    `source_type` carries its ORIGIN and CONFIDENCE beside it. A contract-proposed
--    `PAID_MEDIA` and an operator-confirmed one are different evidence, and
--    `UNKNOWN` is a typed gap that must stay distinguishable from both -- migration
--    119's `datastream_source_types` had one bare column and no writer, so every
--    Datastream was silently type-less and the compiler read that as scope-matched.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.datastream_tax_evidence (
    id                      TEXT        PRIMARY KEY,
    project_id              TEXT        NOT NULL REFERENCES app.projects(id) ON DELETE CASCADE,
    datastream_id           TEXT        NOT NULL REFERENCES app.datastreams(id) ON DELETE CASCADE,
    execution_id            TEXT,

    source_type             TEXT        NOT NULL DEFAULT 'UNKNOWN'
                                CHECK (source_type IN (
                                    'PAID_MEDIA', 'LEAD_GEN_MEDIA', 'DIRECT_SERVICE_COST',
                                    'COMMERCE_REVENUE', 'ORGANIC_ANALYTICS', 'UNKNOWN')),
    source_type_origin      TEXT        NOT NULL DEFAULT 'unresolved'
                                CHECK (source_type_origin IN (
                                    'connector_contract', 'observed_mapping',
                                    'operator_override', 'unresolved')),
    source_type_confidence  TEXT        NOT NULL DEFAULT 'none'
                                CHECK (source_type_confidence IN ('high', 'medium', 'low', 'none')),

    -- AC7: "Revenue tax posture is observed/confirmed per Datastream/publication,
    -- not assumed from Connector identity." `unknown` blocks HT/TTC normalization
    -- rather than defaulting to exclusive, which is what silently produced a
    -- plausible ROAS out of two incompatible numbers.
    tax_posture             TEXT        NOT NULL DEFAULT 'unknown'
                                CHECK (tax_posture IN ('inclusive', 'exclusive', 'not_applicable', 'unknown')),
    tax_posture_origin      TEXT        NOT NULL DEFAULT 'unresolved'
                                CHECK (tax_posture_origin IN (
                                    'observed_field', 'operator_override', 'unresolved')),

    applicability           TEXT        NOT NULL DEFAULT 'unresolved'
                                CHECK (applicability IN (
                                    'applicable', 'not_applicable', 'unavailable',
                                    'excluded', 'unresolved')),

    -- The physical inputs the rules need: native money and currency, impressions,
    -- transaction counts, placement and tax-code fields. Retained as observed
    -- facts with their gaps, never repaired into zeros.
    observed_inputs         JSONB       NOT NULL DEFAULT '{}'::jsonb
                                CHECK (jsonb_typeof(observed_inputs) = 'object'),
    geography_evidence      JSONB       NOT NULL DEFAULT '{}'::jsonb
                                CHECK (jsonb_typeof(geography_evidence) = 'object'),
    gaps                    JSONB       NOT NULL DEFAULT '[]'::jsonb
                                CHECK (jsonb_typeof(gaps) = 'array'),

    plan_version_id         TEXT,
    mapping_version_id      TEXT,
    publication_id          TEXT,

    content_hash            TEXT        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    recorded_by             TEXT        NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (datastream_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_datastream_tax_evidence_latest
    ON app.datastream_tax_evidence (datastream_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_datastream_tax_evidence_project
    ON app.datastream_tax_evidence (project_id, applicability);

CREATE OR REPLACE FUNCTION app.reject_datastream_tax_evidence_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'observed Tax evidence is append-only';
END;
$$;
DROP TRIGGER IF EXISTS trg_datastream_tax_evidence_immutable ON app.datastream_tax_evidence;
CREATE TRIGGER trg_datastream_tax_evidence_immutable
    BEFORE UPDATE OR DELETE ON app.datastream_tax_evidence
    FOR EACH ROW
    -- The erasure hatch, written in at creation rather than three migrations later.
    WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
    EXECUTE FUNCTION app.reject_datastream_tax_evidence_mutation();


-- ---------------------------------------------------------------------------
-- 4. The ONE activation authority, projected (AC1).
--
--    Active means all three of: the capability is not disabled, its pin names the
--    Project's CURRENT active configuration version, and a ladder version is
--    published. A capability pinned to a superseded configuration is NOT active --
--    that is what "cannot diverge" has to mean in SQL, rather than in prose.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW app.tax_fee_rule_set_v AS
SELECT s.project_id,
       s.id                                                    AS rule_set_id,
       v.id                                                    AS rule_set_version_id,
       v.version_number,
       v.content_hash                                          AS rule_set_content_hash,
       COALESCE(v.payload->>'rounding', 'half_even')           AS rounding,
       COALESCE(v.payload->>'default_money_basis', 'native_source') AS default_money_basis,
       jsonb_array_length(COALESCE(v.ordered_rules, '[]'::jsonb))   AS rule_count,
       v.created_by,
       v.created_at
FROM app.governance_rule_sets s
JOIN app.governance_rule_set_versions v
  ON v.id = s.current_version_id
 AND v.rule_set_id = s.id
 AND v.project_id = s.project_id
WHERE s.family = 'tax_fee'
  AND s.lifecycle_status <> 'archived'
  AND v.status = 'published';

CREATE OR REPLACE VIEW app.project_tax_fee_activation_v AS
SELECT p.id                                        AS project_id,
       p.active_configuration_version_id           AS project_configuration_version_id,
       COALESCE(c.state, 'disabled')               AS capability_state,
       (COALESCE(c.state, 'disabled') <> 'disabled'
        AND c.active_version_id IS NOT NULL
        AND c.active_version_id = p.active_configuration_version_id
        AND rs.rule_set_version_id IS NOT NULL)    AS tax_fees_active,
       rs.rule_set_id,
       rs.rule_set_version_id,
       rs.rule_set_content_hash,
       rs.rounding,
       rs.default_money_basis,
       COALESCE(rs.rule_count, 0)                  AS rule_count
FROM app.projects p
LEFT JOIN app.project_capabilities c
       ON c.project_id = p.id AND c.capability_key = 'tax_fees'
LEFT JOIN app.tax_fee_rule_set_v rs
       ON rs.project_id = p.id;


-- ---------------------------------------------------------------------------
-- 5. `fee_tax_alignment_enabled` becomes a projection that cannot diverge.
--
--    Not dropped: the column is the trace of the authority that used to live here,
--    and existing mirrors read it. It is now WRITTEN BY NOBODY -- a direct write
--    raises, and every write forces the governed value. The one door left to
--    enable Tax & Fees is a Project Change Set.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.project_preferences_tax_alignment_projection()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    governed BOOLEAN;
BEGIN
    SELECT a.tax_fees_active INTO governed
    FROM app.project_tax_fee_activation_v a
    WHERE a.project_id = NEW.project_id;
    governed := COALESCE(governed, FALSE);

    IF TG_OP = 'UPDATE'
       AND NEW.fee_tax_alignment_enabled IS DISTINCT FROM OLD.fee_tax_alignment_enabled
       AND NEW.fee_tax_alignment_enabled IS DISTINCT FROM governed THEN
        RAISE EXCEPTION
            'fee_tax_alignment_enabled is a read-only projection of the active '
            'Project Configuration Version; enable Tax & Fees through a Project Change Set';
    END IF;
    IF TG_OP = 'INSERT'
       AND NEW.fee_tax_alignment_enabled IS TRUE
       AND governed IS FALSE THEN
        RAISE EXCEPTION
            'fee_tax_alignment_enabled is a read-only projection of the active '
            'Project Configuration Version; enable Tax & Fees through a Project Change Set';
    END IF;

    NEW.fee_tax_alignment_enabled := governed;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_project_preferences_tax_alignment ON app.project_preferences;
CREATE TRIGGER trg_project_preferences_tax_alignment
    BEFORE INSERT OR UPDATE ON app.project_preferences
    FOR EACH ROW EXECUTE FUNCTION app.project_preferences_tax_alignment_projection();

-- Reconcile whatever the old authority left behind, once.
UPDATE app.project_preferences pp
   SET fee_tax_alignment_enabled = COALESCE(a.tax_fees_active, FALSE)
  FROM app.project_tax_fee_activation_v a
 WHERE a.project_id = pp.project_id
   AND pp.fee_tax_alignment_enabled IS DISTINCT FROM COALESCE(a.tax_fees_active, FALSE);


-- ---------------------------------------------------------------------------
-- 6. The three flattening views, rebased onto the PUBLISHED ladder.
--
--    Same relation names, same frozen scalar columns in the same order, so every
--    dbt model and the mirror keep compiling -- and every one of them now reads
--    immutable governed content instead of a mutable row. The evidence columns are
--    APPENDED after the frozen 22 so a positional reader cannot be surprised.
--
--    `status` is the constant 'confirmed'. A rule inside a published version IS
--    active by virtue of the version; a per-rule status would be the second
--    activation authority AC1 removes. Keeping the column means the dbt
--    `WHERE status = 'confirmed'` filters stay true statements rather than dead code.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW app.tax_fee_published_ladder_v AS
SELECT rs.project_id,
       rs.rule_set_id,
       rs.rule_set_version_id,
       rs.version_number,
       rs.rule_set_content_hash,
       rs.rounding,
       rs.default_money_basis,
       rs.created_by,
       rs.created_at,
       rule.ord::INTEGER AS ladder_position,
       rule.value        AS rule
FROM app.tax_fee_rule_set_v rs
JOIN app.governance_rule_set_versions v
  ON v.id = rs.rule_set_version_id
CROSS JOIN LATERAL jsonb_array_elements(COALESCE(v.ordered_rules, '[]'::jsonb))
     WITH ORDINALITY AS rule(value, ord);

DROP VIEW IF EXISTS app.fee_tax_rules_dim_v;
CREATE VIEW app.fee_tax_rules_dim_v AS
SELECT l.rule_set_version_id || ':' || (l.rule->>'rule_key')  AS id,
       l.project_id,
       l.rule->>'scope_kind'                                  AS scope_kind,
       NULLIF(l.rule->>'scope_ref', '')                       AS scope_ref,
       l.rule->>'category'                                    AS category,
       l.rule->>'form'                                        AS form,
       (l.rule->>'rate')::NUMERIC(12,6)                       AS rate,
       (l.rule->>'amount_micros')::BIGINT                     AS amount_micros,
       (l.rule->>'cpm_micros')::BIGINT                        AS cpm_micros,
       NULLIF(l.rule->>'currency', '')                        AS currency,
       l.rule->>'base_target'                                 AS base_target,
       (l.rule->>'cascade_phase')::INTEGER                    AS cascade_phase,
       (l.rule->>'sequence_order')::INTEGER                   AS sequence_order,
       (l.rule->>'effective_from')::DATE                      AS effective_from,
       (l.rule->>'effective_to')::DATE                        AS effective_to,
       'confirmed'::TEXT                                      AS status,
       l.rule->>'origin'                                      AS origin,
       NULL::TEXT                                             AS dedup_hash,
       l.rule->>'label'                                       AS label,
       l.created_by,
       l.created_at,
       l.created_at                                           AS updated_at,
       -- Evidence, appended. Every one of these is a column a Result row pins so
       -- the number can be reproduced without reading current settings (AC9).
       l.rule_set_id,
       l.rule_set_version_id,
       l.rule_set_content_hash,
       l.rule->>'rule_key'                                    AS rule_key,
       l.ladder_position,
       l.rounding,
       l.rule->>'money_basis'                                 AS money_basis,
       l.rule->>'authority_kind'                              AS authority_kind,
       l.rule->'jurisdiction'->>'kind'                        AS jurisdiction_kind,
       l.rule->'jurisdiction'->>'id'                          AS jurisdiction_id,
       l.rule->'jurisdiction'->>'hierarchy_version_id'        AS geography_hierarchy_version_id,
       COALESCE((l.rule->>'geography_dependent')::BOOLEAN, FALSE) AS geography_dependent,
       l.rule->>'rest_of_world_posture'                       AS rest_of_world_posture,
       l.rule->>'unknown_posture'                             AS unknown_posture,
       l.rule->'source_evidence'->>'issuer'                   AS source_issuer,
       l.rule->'source_evidence'->>'reference'                AS source_reference,
       l.rule->'source_evidence'->>'reference_version'        AS source_reference_version,
       l.rule->'source_evidence'->>'preset_version_id'        AS source_preset_version_id,
       (l.rule->'source_evidence'->>'published_on')::DATE     AS source_published_on
FROM app.tax_fee_published_ladder_v l;

DROP VIEW IF EXISTS app.fee_tax_rule_conditions_v;
CREATE VIEW app.fee_tax_rule_conditions_v AS
SELECT l.rule_set_version_id || ':' || (l.rule->>'rule_key') AS rule_id,
       cond.key                                              AS condition_key,
       val.value                                             AS condition_value
FROM app.tax_fee_published_ladder_v l
CROSS JOIN LATERAL jsonb_each(COALESCE(l.rule->'conditions', '{}'::jsonb)) AS cond(key, value)
CROSS JOIN LATERAL jsonb_array_elements_text(cond.value)                   AS val(value)
UNION ALL
-- source_type_scope rides as rows with condition_key = 'source_type', exactly as
-- migration 119 shaped it: ONE uniform matching mechanism, and no array crosses
-- the mirror. An empty scope yields no rows -- absence reads as "applies to
-- everything", never as "matches nothing".
SELECT l.rule_set_version_id || ':' || (l.rule->>'rule_key') AS rule_id,
       'source_type'::TEXT                                   AS condition_key,
       scope.value                                           AS condition_value
FROM app.tax_fee_published_ladder_v l
CROSS JOIN LATERAL jsonb_array_elements_text(
        COALESCE(l.rule->'source_type_scope', '[]'::jsonb)) AS scope(value);

DROP VIEW IF EXISTS app.fee_tax_rule_tiers_v;
CREATE VIEW app.fee_tax_rule_tiers_v AS
SELECT l.rule_set_version_id || ':' || (l.rule->>'rule_key') AS rule_id,
       band.ord::INTEGER - 1                                 AS tier_index,
       (band.value->>'threshold_micros')::BIGINT             AS threshold_micros,
       (band.value->>'rate')::NUMERIC(12,6)                  AS rate,
       COALESCE(l.rule->'tiers'->>'mode', 'cliff')           AS mode
FROM app.tax_fee_published_ladder_v l
CROSS JOIN LATERAL jsonb_array_elements(l.rule->'tiers'->'bands')
     WITH ORDINALITY AS band(value, ord)
WHERE l.rule->'tiers' IS NOT NULL
  AND jsonb_typeof(l.rule->'tiers') = 'object';


-- ---------------------------------------------------------------------------
-- 7. The Datastream source-type projection, rebased onto observed evidence.
--
--    `app.datastream_source_types` had no production writer, so the mirror carried
--    an empty table and the compiler read "no declared type" as "scope matched".
--    The view keeps the mirror's relation name and shape; its rows now come from
--    the LATEST observed evidence per Datastream, and a Datastream with no
--    publication produces no row at all -- which is a gap the compiler must handle,
--    not a type it may assume.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW app.datastream_source_types_v AS
SELECT DISTINCT ON (e.datastream_id)
       e.datastream_id,
       e.project_id,
       e.source_type,
       e.source_type_origin,
       e.source_type_confidence,
       e.tax_posture,
       e.applicability,
       e.id            AS tax_evidence_version_id,
       e.created_at
FROM app.datastream_tax_evidence e
ORDER BY e.datastream_id, e.created_at DESC;

COMMIT;
