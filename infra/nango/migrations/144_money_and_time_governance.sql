-- Story 48.3: the governed owners for Currency & FX and Reporting Timezone.
--
-- The baseline this migration corrects, stated exactly:
--
--   * `app.project_preferences.canonical_currency` DEFAULT 'EUR' and
--     `reporting_timezone` DEFAULT 'Europe/Paris' (migration 008) are a SECOND
--     authority, and a column default is the worst kind: every Project has a
--     non-null reporting currency from birth, so `currency IS NOT NULL` reads as
--     "confirmed" everywhere and no code can tell a decision from a default.
--     Migration 131 added `*_confirmation_status`, which made the distinction
--     EXPRESSIBLE but left the value populated -- so the two compilers still read
--     the column and still emitted a content hash as the owner version.
--   * There is no home at all for an FX policy, a rate set, a rate observation,
--     a timezone policy or the tzdb version a derivation used. `dbt/seeds/fx_rates.csv`
--     was serving as runtime FX authority (a CSV; editable; unversioned).
--   * There is no home for OBSERVED per-publication evidence: what currency,
--     unit and day boundary a pull actually landed. Coverage was therefore
--     compiled from a Connector *declaration*, which states what a source CAN do,
--     not what it DID.
--
-- WHY THE RULE SET TABLES ARE GENERIC. Story 49.4 owns Controls & Quality and
-- names `governance_rule_sets` / `governance_rule_set_versions` /
-- `rule_set_exceptions` in its Technical Contract. It has not landed. Story 48.3's
-- Implementation Gate forbids building "a temporary currency-specific or
-- timezone-specific Governance lifecycle and calling it complete", so this
-- migration creates 49.4's SHAPE -- family-typed, opaque `family`, no branch on
-- its value anywhere in SQL or in `core.governance_rule_sets` -- and mounts Money,
-- FX ingestion and Timezone on it as three families among the ones 49.4 will add
-- (reconciliation, dq, capability). 49.4 adopts these tables; it does not rebuild
-- them, and it does not inherit a Money-shaped lifecycle either.
--
-- Idempotent: IF NOT EXISTS / OR REPLACE throughout, so re-application is a no-op.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. The generic Governance Rule Set (Story 49.4 shape).
--
--    Three distinct version pointers, for the reason 140 and 142 both state: a
--    failed publication that silently moves `current` is indistinguishable from
--    one that worked, and `last_known_good` is what a rollback reads.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.governance_rule_sets (
    id                          TEXT        PRIMARY KEY
                                CHECK (id ~ '^grs_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id                      TEXT        NOT NULL
                                REFERENCES app.organizations(id) ON DELETE RESTRICT,
    project_id                  TEXT        NOT NULL
                                REFERENCES app.projects(id) ON DELETE CASCADE,

    -- Opaque to this schema and to core.governance_rule_sets (AD-2). `money_policy`
    -- is one family, not the model.
    family                      TEXT        NOT NULL
                                CHECK (family ~ '^[a-z][a-z0-9_]{1,39}$'),
    name                        TEXT        NOT NULL
                                CHECK (name ~ '^[a-z][a-z0-9_]{0,126}$'),
    label                       TEXT        NOT NULL
                                CHECK (length(btrim(label)) BETWEEN 1 AND 160),

    -- The exact selector this rule set applies to, validated by the family's
    -- profile, never interpreted here.
    scope                       JSONB       NOT NULL DEFAULT '{}'::jsonb
                                CHECK (jsonb_typeof(scope) = 'object'),

    lifecycle_status            TEXT        NOT NULL DEFAULT 'draft'
                                CHECK (lifecycle_status IN ('draft', 'candidate', 'published',
                                                            'superseded', 'archived')),
    current_version_id          TEXT,
    pending_version_id          TEXT,
    last_known_good_version_id  TEXT,

    created_by                  TEXT        NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_governance_rule_sets_scope UNIQUE (id, project_id)
);

-- One head per (Project, family, name); an archived one frees the name again.
CREATE UNIQUE INDEX IF NOT EXISTS uq_governance_rule_sets_name
    ON app.governance_rule_sets (project_id, family, name)
    WHERE lifecycle_status <> 'archived';

CREATE INDEX IF NOT EXISTS idx_governance_rule_sets_family
    ON app.governance_rule_sets (project_id, family, lifecycle_status);

CREATE TABLE IF NOT EXISTS app.governance_rule_set_versions (
    id                      TEXT        PRIMARY KEY
                            CHECK (id ~ '^grsv_[0-9A-HJKMNP-TV-Z]{26}$'),
    rule_set_id             TEXT        NOT NULL,
    project_id              TEXT        NOT NULL,
    version_number          INTEGER     NOT NULL CHECK (version_number >= 1),
    status                  TEXT        NOT NULL
                            CHECK (status IN ('draft', 'candidate', 'published',
                                              'superseded', 'archived')),
    family                  TEXT        NOT NULL,
    profile                 TEXT        NOT NULL
                            CHECK (profile ~ '^[a-z][a-z0-9_]{1,63}$'),
    label                   TEXT        NOT NULL,
    description             TEXT,

    -- The complete typed content. A version that referred back to the mutable
    -- head would not be a version (the argument migration 142 makes for
    -- semantic_concept_versions, and it holds identically here).
    payload                 JSONB       NOT NULL
                            CHECK (jsonb_typeof(payload) = 'object'),
    -- Ordered, typed rules where the family has any. Order is content, not a
    -- last-write accident: 49.4's profile contract forbids resolving a tie by
    -- write order, so the sequence lives in the immutable version.
    ordered_rules           JSONB       NOT NULL DEFAULT '[]'::jsonb
                            CHECK (jsonb_typeof(ordered_rules) = 'array'),

    -- Exact owner versions this content depends on: Semantic Concept versions,
    -- Master Data vocabulary versions, Project configuration versions. A
    -- dependency on "the latest X" is not a dependency.
    requires                JSONB       NOT NULL DEFAULT '[]'::jsonb
                            CHECK (jsonb_typeof(requires) = 'array'),

    effective_from          DATE,
    effective_to            DATE,
    CONSTRAINT ck_governance_rule_set_versions_interval
        CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from),

    content_hash            TEXT        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    created_by              TEXT        NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_governance_rule_set_version UNIQUE (rule_set_id, version_number),
    CONSTRAINT uq_governance_rule_set_version_scope UNIQUE (id, rule_set_id, project_id),
    CONSTRAINT fk_governance_rule_set_version_scope
        FOREIGN KEY (rule_set_id, project_id)
        REFERENCES app.governance_rule_sets (id, project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_governance_rule_set_versions_head
    ON app.governance_rule_set_versions (rule_set_id, version_number DESC);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_governance_rule_sets_current_version'
          AND conrelid = 'app.governance_rule_sets'::regclass
    ) THEN
        ALTER TABLE app.governance_rule_sets
            ADD CONSTRAINT fk_governance_rule_sets_current_version
            FOREIGN KEY (current_version_id, id, project_id)
            REFERENCES app.governance_rule_set_versions (id, rule_set_id, project_id)
            DEFERRABLE INITIALLY IMMEDIATE;
        ALTER TABLE app.governance_rule_sets
            ADD CONSTRAINT fk_governance_rule_sets_pending_version
            FOREIGN KEY (pending_version_id, id, project_id)
            REFERENCES app.governance_rule_set_versions (id, rule_set_id, project_id)
            DEFERRABLE INITIALLY IMMEDIATE;
        ALTER TABLE app.governance_rule_sets
            ADD CONSTRAINT fk_governance_rule_sets_lkg_version
            FOREIGN KEY (last_known_good_version_id, id, project_id)
            REFERENCES app.governance_rule_set_versions (id, rule_set_id, project_id)
            DEFERRABLE INITIALLY IMMEDIATE;
    END IF;
END $$;

-- Published is frozen. Draft and candidate may still be edited in place; the only
-- transitions out of published are the two status-only ones.
CREATE OR REPLACE FUNCTION app.reject_rule_set_version_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status IN ('published', 'superseded', 'archived') THEN
            RAISE EXCEPTION 'published rule set versions are immutable'
                USING ERRCODE = '23000';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status IN ('superseded', 'archived') THEN
        RAISE EXCEPTION 'published rule set versions are immutable'
            USING ERRCODE = '23000';
    END IF;
    IF OLD.status = 'published' THEN
        IF NEW.status NOT IN ('superseded', 'archived') THEN
            RAISE EXCEPTION 'published rule set versions are immutable'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.id <> OLD.id
            OR NEW.rule_set_id IS DISTINCT FROM OLD.rule_set_id
            OR NEW.content_hash <> OLD.content_hash
            OR NEW.version_number <> OLD.version_number
            OR NEW.payload::text <> OLD.payload::text
        THEN
            RAISE EXCEPTION 'published rule set versions are immutable'
                USING ERRCODE = '23000';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_governance_rule_set_versions_immutable
    ON app.governance_rule_set_versions;
CREATE TRIGGER trg_governance_rule_set_versions_immutable
    BEFORE UPDATE OR DELETE ON app.governance_rule_set_versions
    FOR EACH ROW EXECUTE FUNCTION app.reject_rule_set_version_mutation();

-- Version-bound decisions: who exempted what, why, and for how long. An exception
-- without an interval is a permanent silent hole, so the interval is required.
CREATE TABLE IF NOT EXISTS app.governance_rule_set_exceptions (
    id                  TEXT        PRIMARY KEY
                        CHECK (id ~ '^grse_[0-9A-HJKMNP-TV-Z]{26}$'),
    project_id          TEXT        NOT NULL REFERENCES app.projects(id) ON DELETE CASCADE,
    rule_set_id         TEXT        NOT NULL,
    rule_set_version_id TEXT        NOT NULL
                        REFERENCES app.governance_rule_set_versions (id) ON DELETE RESTRICT,
    subject_kind        TEXT        NOT NULL
                        CHECK (subject_kind ~ '^[a-z][a-z0-9_]{1,39}$'),
    subject_id          TEXT        NOT NULL,
    reason_code         TEXT        NOT NULL
                        CHECK (reason_code ~ '^[a-z][a-z0-9_]{1,63}$'),
    reason              TEXT        NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 600),
    effective_from      DATE        NOT NULL,
    expires_at          DATE,
    decided_by          TEXT        NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_governance_rule_set_exceptions_interval
        CHECK (expires_at IS NULL OR expires_at >= effective_from),
    CONSTRAINT fk_governance_rule_set_exception_scope
        FOREIGN KEY (rule_set_id, project_id)
        REFERENCES app.governance_rule_sets (id, project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_governance_rule_set_exceptions_subject
    ON app.governance_rule_set_exceptions (project_id, subject_kind, subject_id);

-- ---------------------------------------------------------------------------
-- 2. FX Rate Sets. Immutable observations inside a versioned, validated set.
--
--    `rate` is NUMERIC(38,18), not DOUBLE. A binary float cannot represent a
--    published rate exactly, so two systems reading "the same rate" compute two
--    different totals -- which is precisely the class of defect the Currency &
--    FX criterion "a cross-currency total succeeds without explicit FX evidence"
--    describes from the other end.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.fx_rate_sets (
    id                          TEXT        PRIMARY KEY
                                CHECK (id ~ '^fxs_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id                      TEXT        NOT NULL
                                REFERENCES app.organizations(id) ON DELETE RESTRICT,
    project_id                  TEXT        NOT NULL
                                REFERENCES app.projects(id) ON DELETE CASCADE,
    label                       TEXT        NOT NULL
                                CHECK (length(btrim(label)) BETWEEN 1 AND 160),
    -- The ingestion policy that governs refresh, priority, staleness and
    -- fallback. A rate set with no governing policy would activate batches under
    -- rules nobody confirmed.
    ingestion_rule_set_id       TEXT,
    current_version_id          TEXT,
    pending_version_id          TEXT,
    last_known_good_version_id  TEXT,
    created_by                  TEXT        NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_fx_rate_sets_scope UNIQUE (id, project_id),
    CONSTRAINT fk_fx_rate_sets_ingestion_policy
        FOREIGN KEY (ingestion_rule_set_id, project_id)
        REFERENCES app.governance_rule_sets (id, project_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_fx_rate_sets_project
    ON app.fx_rate_sets (project_id);

CREATE TABLE IF NOT EXISTS app.fx_rate_set_versions (
    id                  TEXT        PRIMARY KEY
                        CHECK (id ~ '^fxsv_[0-9A-HJKMNP-TV-Z]{26}$'),
    rate_set_id         TEXT        NOT NULL,
    project_id          TEXT        NOT NULL,
    version_number      INTEGER     NOT NULL CHECK (version_number >= 1),

    -- `validated` is the only status a read may consume. `ingesting` is a batch
    -- still landing; `rejected` failed validation and stays for the audit trail.
    status              TEXT        NOT NULL
                        CHECK (status IN ('ingesting', 'validated', 'rejected', 'superseded')),
    -- The business day this batch speaks for, and the moment it was retrieved.
    as_of_date          DATE        NOT NULL,
    retrieved_at        TIMESTAMPTZ NOT NULL,
    -- Free-form provider identity, resolved from the ingestion policy. Never a
    -- connector name and never branched on in core.
    provider            TEXT        NOT NULL CHECK (length(btrim(provider)) BETWEEN 1 AND 120),
    provider_reference  TEXT,
    method              TEXT        NOT NULL
                        CHECK (method IN ('published_batch', 'manual_entry', 'fixture')),
    validation          JSONB       NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(validation) = 'object'),
    observation_count   INTEGER     NOT NULL CHECK (observation_count >= 0),
    content_hash        TEXT        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    created_by          TEXT        NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_fx_rate_set_version UNIQUE (rate_set_id, version_number),
    CONSTRAINT uq_fx_rate_set_version_scope UNIQUE (id, rate_set_id, project_id),
    -- Deterministic: re-ingesting an identical batch resolves to the stored row
    -- rather than minting a rival version of identical content.
    CONSTRAINT uq_fx_rate_set_version_content UNIQUE (rate_set_id, content_hash),
    CONSTRAINT fk_fx_rate_set_version_scope
        FOREIGN KEY (rate_set_id, project_id)
        REFERENCES app.fx_rate_sets (id, project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_fx_rate_set_versions_asof
    ON app.fx_rate_set_versions (rate_set_id, as_of_date DESC, version_number DESC);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_fx_rate_sets_current_version'
          AND conrelid = 'app.fx_rate_sets'::regclass
    ) THEN
        ALTER TABLE app.fx_rate_sets
            ADD CONSTRAINT fk_fx_rate_sets_current_version
            FOREIGN KEY (current_version_id, id, project_id)
            REFERENCES app.fx_rate_set_versions (id, rate_set_id, project_id)
            DEFERRABLE INITIALLY IMMEDIATE;
        ALTER TABLE app.fx_rate_sets
            ADD CONSTRAINT fk_fx_rate_sets_pending_version
            FOREIGN KEY (pending_version_id, id, project_id)
            REFERENCES app.fx_rate_set_versions (id, rate_set_id, project_id)
            DEFERRABLE INITIALLY IMMEDIATE;
        ALTER TABLE app.fx_rate_sets
            ADD CONSTRAINT fk_fx_rate_sets_lkg_version
            FOREIGN KEY (last_known_good_version_id, id, project_id)
            REFERENCES app.fx_rate_set_versions (id, rate_set_id, project_id)
            DEFERRABLE INITIALLY IMMEDIATE;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS app.fx_rate_observations (
    id                  TEXT        PRIMARY KEY
                        CHECK (id ~ '^fxo_[0-9A-HJKMNP-TV-Z]{26}$'),
    rate_set_version_id TEXT        NOT NULL
                        REFERENCES app.fx_rate_set_versions (id) ON DELETE CASCADE,
    project_id          TEXT        NOT NULL,

    base_currency       TEXT        NOT NULL CHECK (base_currency ~ '^[A-Z]{3}$'),
    quote_currency      TEXT        NOT NULL CHECK (quote_currency ~ '^[A-Z]{3}$'),
    -- Exact. 18 fractional digits covers every published quotation, including
    -- the hyperinflated pairs where a DOUBLE loses the low-order digits entirely.
    rate                NUMERIC(38, 18) NOT NULL CHECK (rate > 0),
    effective_date      DATE        NOT NULL,
    as_of_date          DATE        NOT NULL,

    -- The four derivations, distinguishable as AC4 requires. `identity` is the
    -- same-currency case and is EVIDENCE, not a COALESCE default.
    method              TEXT        NOT NULL
                        CHECK (method IN ('direct', 'triangulated', 'carry_forward', 'identity')),
    -- For a triangulation, the pivot and the two legs actually used; for a
    -- carry-forward, the observation it was carried from. Empty for `direct`.
    derivation          JSONB       NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(derivation) = 'object'),
    -- Who contributed the quotation, in priority order.
    source_contributors JSONB       NOT NULL DEFAULT '[]'::jsonb
                        CHECK (jsonb_typeof(source_contributors) = 'array'),
    retrieved_at        TIMESTAMPTZ NOT NULL,
    validation_status   TEXT        NOT NULL DEFAULT 'validated'
                        CHECK (validation_status IN ('validated', 'rejected')),
    content_hash        TEXT        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),

    CONSTRAINT uq_fx_rate_observation
        UNIQUE (rate_set_version_id, base_currency, quote_currency, effective_date),
    CONSTRAINT ck_fx_rate_observation_identity
        CHECK (method <> 'identity' OR (base_currency = quote_currency AND rate = 1))
);

CREATE INDEX IF NOT EXISTS idx_fx_rate_observations_pair
    ON app.fx_rate_observations
       (rate_set_version_id, base_currency, quote_currency, effective_date DESC);

CREATE OR REPLACE FUNCTION app.reject_fx_observation_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'FX rate observations are immutable'
        USING ERRCODE = '23000';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_fx_rate_observations_immutable ON app.fx_rate_observations;
CREATE TRIGGER trg_fx_rate_observations_immutable
    BEFORE UPDATE ON app.fx_rate_observations
    FOR EACH ROW EXECUTE FUNCTION app.reject_fx_observation_mutation();

-- A validated rate-set version is frozen too: its observations are the evidence
-- a Result pinned, so re-deriving under the same version must give the same
-- number forever. Status may still move validated -> superseded.
CREATE OR REPLACE FUNCTION app.reject_fx_rate_set_version_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status IN ('validated', 'superseded') THEN
            RAISE EXCEPTION 'validated FX rate set versions are immutable'
                USING ERRCODE = '23000';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status = 'superseded' THEN
        RAISE EXCEPTION 'validated FX rate set versions are immutable'
            USING ERRCODE = '23000';
    END IF;
    IF OLD.status = 'validated' THEN
        IF NEW.status <> 'superseded'
            OR NEW.content_hash <> OLD.content_hash
            OR NEW.as_of_date <> OLD.as_of_date
            OR NEW.observation_count <> OLD.observation_count
        THEN
            RAISE EXCEPTION 'validated FX rate set versions are immutable'
                USING ERRCODE = '23000';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_fx_rate_set_versions_immutable ON app.fx_rate_set_versions;
CREATE TRIGGER trg_fx_rate_set_versions_immutable
    BEFORE UPDATE OR DELETE ON app.fx_rate_set_versions
    FOR EACH ROW EXECUTE FUNCTION app.reject_fx_rate_set_version_mutation();

-- ---------------------------------------------------------------------------
-- 3. OBSERVED per-publication evidence (Data-owned).
--
--    A Connector descriptor declares what a source CAN do. These two tables
--    record what a pull ACTUALLY landed. AC6 turns on exactly that difference:
--    "the compiler never marks coverage complete from the descriptor alone".
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.datastream_money_evidence (
    id                  TEXT        PRIMARY KEY
                        CHECK (id ~ '^dsme_[0-9A-HJKMNP-TV-Z]{26}$'),
    project_id          TEXT        NOT NULL REFERENCES app.projects(id) ON DELETE CASCADE,
    datastream_id       TEXT        NOT NULL,
    -- The exact publication this evidence was observed on. NULL only for an
    -- evidence row recorded before any publication, which is `unavailable`
    -- coverage, never `complete`.
    execution_id        TEXT,
    plan_version_id     TEXT,
    mapping_version_id  TEXT,

    -- [{canonical_field, native_currency, native_unit, adapter, sample_count,
    --   distinct_currencies[]}] -- what the rows actually carried.
    observed_fields     JSONB       NOT NULL DEFAULT '[]'::jsonb
                        CHECK (jsonb_typeof(observed_fields) = 'array'),
    -- Every field whose currency, unit or adapter could NOT be established.
    -- A typed gap, retained; never repaired into a default.
    gaps                JSONB       NOT NULL DEFAULT '[]'::jsonb
                        CHECK (jsonb_typeof(gaps) = 'array'),
    confidence          TEXT        NOT NULL DEFAULT 'observed'
                        CHECK (confidence IN ('observed', 'declared', 'assumed', 'unknown')),
    assumptions         JSONB       NOT NULL DEFAULT '[]'::jsonb
                        CHECK (jsonb_typeof(assumptions) = 'array'),
    -- Where, if anywhere, the operator can change the source's own currency.
    source_lever        JSONB       NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(source_lever) = 'object'),

    content_hash        TEXT        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    observed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_datastream_money_evidence
        UNIQUE (datastream_id, execution_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_datastream_money_evidence_latest
    ON app.datastream_money_evidence (project_id, datastream_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS app.datastream_time_boundary_evidence (
    id                      TEXT        PRIMARY KEY
                            CHECK (id ~ '^dstbe_[0-9A-HJKMNP-TV-Z]{26}$'),
    project_id              TEXT        NOT NULL REFERENCES app.projects(id) ON DELETE CASCADE,
    datastream_id           TEXT        NOT NULL,
    execution_id            TEXT,
    plan_version_id         TEXT,

    -- The distinction AC7 is built on: a source DATE with no timestamp can never
    -- be re-aligned, and a timestamp-grain fact sometimes can.
    grain                   TEXT        NOT NULL
                            CHECK (grain IN ('date_only', 'timestamp', 'unknown')),
    timestamp_sufficiency   TEXT        NOT NULL
                            CHECK (timestamp_sufficiency IN ('sufficient',
                                                             'insufficient_no_timestamp',
                                                             'insufficient_no_source_zone',
                                                             'unknown')),
    observed_report_timezone TEXT,
    -- Where the zone came from. `declaration` alone can never make coverage
    -- complete -- that is the whole point of this table.
    evidence_origin         TEXT        NOT NULL
                            CHECK (evidence_origin IN ('pull_metadata', 'publication_probe',
                                                       'declaration', 'operator_statement',
                                                       'none')),
    confidence              TEXT        NOT NULL DEFAULT 'observed'
                            CHECK (confidence IN ('observed', 'declared', 'assumed', 'unknown')),
    assumed                 BOOLEAN     NOT NULL DEFAULT FALSE,
    assumptions             JSONB       NOT NULL DEFAULT '[]'::jsonb
                            CHECK (jsonb_typeof(assumptions) = 'array'),
    -- {available: bool, locus: text, hint: text} -- the source-side lever the
    -- Reporting Timezone criterion requires the user to be able to see.
    adjustment_lever        JSONB       NOT NULL DEFAULT '{}'::jsonb
                            CHECK (jsonb_typeof(adjustment_lever) = 'object'),
    tzdb_version            TEXT,
    gap_code                TEXT,

    content_hash            TEXT        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    observed_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_datastream_time_boundary_evidence
        UNIQUE (datastream_id, execution_id, content_hash),
    -- An observed zone that claims pull evidence must name one; a row claiming
    -- `pull_metadata` with no zone is a declaration wearing a costume.
    CONSTRAINT ck_datastream_time_boundary_origin
        CHECK (evidence_origin <> 'pull_metadata' OR observed_report_timezone IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_datastream_time_boundary_evidence_latest
    ON app.datastream_time_boundary_evidence (project_id, datastream_id, observed_at DESC);

-- ---------------------------------------------------------------------------
-- 4. Retire the two column defaults that made a default indistinguishable from
--    a decision.
--
--    The values already stored are NOT erased: an existing Project keeps the
--    value it has, and `*_confirmation_status` (migration 131) already says
--    whether anyone confirmed it. What stops is NEW rows being born confirmed-
--    looking. `NOT NULL` is dropped with the default, because "this Project has
--    not chosen a reporting currency yet" has to be expressible -- that is the
--    `Not applicable` / typed-gap state AC1 requires and the schema forbade.
-- ---------------------------------------------------------------------------

ALTER TABLE app.project_preferences
    ALTER COLUMN canonical_currency DROP DEFAULT;
ALTER TABLE app.project_preferences
    ALTER COLUMN canonical_currency DROP NOT NULL;
ALTER TABLE app.project_preferences
    ALTER COLUMN reporting_timezone DROP DEFAULT;
ALTER TABLE app.project_preferences
    ALTER COLUMN reporting_timezone DROP NOT NULL;

COMMENT ON COLUMN app.project_preferences.canonical_currency IS
    'Story 48.3: a DERIVED PROJECTION of the confirmed Project Configuration Version, never an authority. NULL means no reporting currency has been confirmed. Read it only together with canonical_currency_confirmation_status.';
COMMENT ON COLUMN app.project_preferences.reporting_timezone IS
    'Story 48.3: a DERIVED PROJECTION of the confirmed Project Configuration Version, never an authority. NULL means no reporting timezone has been confirmed.';

COMMENT ON TABLE app.governance_rule_sets IS
    'Story 48.3, in the Story 49.4 shape: the generic Project-owned Rule Set head. `family` is opaque -- Money Policy, FX ingestion and Timezone Policy are three families, and 49.4 adds reconciliation, DQ and capability families to the same tables.';
COMMENT ON TABLE app.fx_rate_observations IS
    'Story 48.3 AC4: immutable exact FX evidence. `method` distinguishes direct, triangulated, carry-forward and identity, so a same-currency 1.0 is evidence rather than a COALESCE fallback.';
COMMENT ON TABLE app.datastream_time_boundary_evidence IS
    'Story 48.3 AC6: what a publication ACTUALLY landed, as opposed to what its Connector descriptor declares it could. Coverage may not be marked complete from a declaration alone.';

COMMIT;
