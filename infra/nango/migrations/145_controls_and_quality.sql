-- Story 49.4: one Controls & Quality authority -- Control Case, DQ Monitor, and
-- the approvals the Rule Set substrate still lacked.
--
-- WHAT ALREADY EXISTS, AND WHY THIS MIGRATION IS SMALLER THAN THE STORY IMPLIES.
-- Migration 144 (Story 48.3) created `governance_rule_sets`,
-- `governance_rule_set_versions` and `governance_rule_set_exceptions` in the
-- shape THIS story specifies -- family-typed, `family` opaque, published versions
-- frozen by trigger, three distinct version pointers. Story 48.3 mounted Money,
-- FX ingestion and Timezone on it; Story 48.4 has since mounted Tax & Fees as a
-- fourth family. So the Rule Set half of 49.4 is not rebuilt here: it is
-- COMPLETED, with the one object 144 did not carry (`rule_set_approvals`) and the
-- two profiles 49.4 itself owns (metric reconciliation and DQ policy, registered
-- in `core.controls_quality`).
--
-- THE MIGRATION MAP, MEASURED RATHER THAN ASSUMED.
-- The Implementation Gate requires an explicit map from resolvable string
-- identifiers to exact references, and a refusal for ambiguous equal-label
-- mappings. Measured on this database on 2026-07-30:
--
--     app.reconciliation_rules      0 rows
--     app.overlap_groups            0 rows
--     app.overlap_group_members     0 rows
--     app.dq_baselines              0 rows
--     app.fx_conflict_resolutions   0 rows
--     app.metric_procedures         0 rows
--     app.mapping_proposals         0 rows
--
-- Every superseded store is EMPTY, so nothing is migrated here and no label is
-- resolved to an id. That is a fact about this deployment, not a property of the
-- design: step 8 below still REFUSES rather than guesses, so a deployment that
-- does hold rows aborts with the exact rows named instead of silently inventing
-- references. The refusal is the deliverable; zero rows is the measurement.
--
-- WHAT IS NOT DONE HERE. Dropping the superseded tables. They are empty and they
-- are the inventory of what still reads them (`metric_semantics.py`,
-- `conflict_resolutions.py`, `dq_monitors.py`); deleting them before those
-- readers move would destroy the only trace of the remaining work, which the
-- repository rules forbid. They stop being AUTHORITIES in this story; the DROP
-- belongs to the commit that removes the last reader.
--
-- Idempotent: IF NOT EXISTS / OR REPLACE throughout.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Rule Set approvals. The one object migration 144 did not carry.
--
--    An exception says "this subject is exempt". An approval says "a human with
--    authority accepted this version". They are different records with different
--    actors, and collapsing them makes "who approved the ladder we are billing
--    on?" unanswerable.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.rule_set_approvals (
    id                  TEXT        PRIMARY KEY
                        CHECK (id ~ '^rsa_[0-9A-HJKMNP-TV-Z]{26}$'),
    project_id          TEXT        NOT NULL REFERENCES app.projects(id) ON DELETE CASCADE,
    rule_set_id         TEXT        NOT NULL,
    rule_set_version_id TEXT        NOT NULL
                        REFERENCES app.governance_rule_set_versions (id) ON DELETE RESTRICT,
    decision            TEXT        NOT NULL CHECK (decision IN ('approved', 'rejected')),
    reason              TEXT        NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 600),
    decided_by          TEXT        NOT NULL,
    decided_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- The exact confirmation that authorized it. A decision with no confirmation
    -- is an assertion that someone approved, not evidence of it.
    confirmation_id     TEXT,
    operation_id        TEXT,
    CONSTRAINT uq_rule_set_approval UNIQUE (rule_set_version_id, decided_by, decided_at),
    CONSTRAINT fk_rule_set_approval_scope
        FOREIGN KEY (rule_set_id, project_id)
        REFERENCES app.governance_rule_sets (id, project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_rule_set_approvals_version
    ON app.rule_set_approvals (rule_set_version_id, decided_at DESC);

-- An approval is a decision. It is never edited and never deleted.
CREATE OR REPLACE FUNCTION app.reject_control_record_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'governance decision records are append-only'
        USING ERRCODE = '23000';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_rule_set_approvals_append_only ON app.rule_set_approvals;
CREATE TRIGGER trg_rule_set_approvals_append_only
    BEFORE UPDATE OR DELETE ON app.rule_set_approvals
    FOR EACH ROW EXECUTE FUNCTION app.reject_control_record_mutation();

-- ---------------------------------------------------------------------------
-- 2. Control Case: the stable head.
--
--    `root_cause_fingerprint` is what makes recurrence expressible. AC2 requires
--    that "recurrence never overwrites history" and resolves by ONE documented
--    deterministic rule. The rule, stated here and enforced by the partial unique
--    index below: at most one OPEN case per (Project, root cause). A producer
--    that observes the same root cause again appends an occurrence to that case;
--    a producer that observes it after the case was closed opens a NEW case
--    linked by `supersedes_case_id`. Neither path edits an occurrence.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.control_cases (
    id                      TEXT        PRIMARY KEY
                            CHECK (id ~ '^ctc_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id                  TEXT        NOT NULL
                            REFERENCES app.organizations(id) ON DELETE RESTRICT,
    project_id              TEXT        NOT NULL
                            REFERENCES app.projects(id) ON DELETE CASCADE,

    -- Opaque producer kind (mapping conflict, semantic ambiguity, unruled
    -- overlap, Master Data alias collision, capability candidate, DQ escalation).
    -- No SQL here branches on its value.
    case_type               TEXT        NOT NULL
                            CHECK (case_type ~ '^[a-z][a-z0-9_]{1,47}$'),
    -- The typed thing the case is ABOUT. Never a label: a case whose subject is
    -- a name breaks the moment the name changes.
    subject_kind            TEXT        NOT NULL
                            CHECK (subject_kind ~ '^[a-z][a-z0-9_]{1,39}$'),
    subject_id              TEXT        NOT NULL,

    root_cause_fingerprint  TEXT        NOT NULL CHECK (root_cause_fingerprint ~ '^[0-9a-f]{64}$'),
    severity                TEXT        NOT NULL
                            CHECK (severity IN ('blocking', 'degrading', 'informational')),
    status                  TEXT        NOT NULL DEFAULT 'open'
                            CHECK (status IN ('open', 'investigating', 'decided',
                                              'resolved', 'closed')),
    owner                   TEXT,

    first_observed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_observed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- How far back the evidence attached to this case is still meaningful. A case
    -- whose evidence has aged out is not silently healthy; it is a case whose
    -- decision must be re-taken on fresh evidence.
    evidence_horizon_at     TIMESTAMPTZ,

    supersedes_case_id      TEXT REFERENCES app.control_cases(id) ON DELETE SET NULL,

    created_by              TEXT        NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_control_cases_scope UNIQUE (id, project_id)
);

-- The recurrence rule, as a constraint rather than a convention.
CREATE UNIQUE INDEX IF NOT EXISTS uq_control_cases_open_root_cause
    ON app.control_cases (project_id, root_cause_fingerprint)
    WHERE status <> 'closed';

CREATE INDEX IF NOT EXISTS idx_control_cases_attention
    ON app.control_cases (project_id, status, severity, last_observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_control_cases_subject
    ON app.control_cases (project_id, subject_kind, subject_id);

DROP TRIGGER IF EXISTS trg_control_cases_updated_at ON app.control_cases;
CREATE TRIGGER trg_control_cases_updated_at
    BEFORE UPDATE ON app.control_cases
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- ---------------------------------------------------------------------------
-- 3. The four append-only episode tables.
--
--    Separate tables, not one untyped event log. 49.4 says so explicitly, and the
--    reason is that they carry different obligations: an occurrence is what was
--    seen, a candidate is what could be done, an impact is what it would cost,
--    and a decision is what was chosen and by whom. One `payload JSONB` column
--    would make every one of those unenforceable.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.control_case_occurrences (
    id              TEXT        PRIMARY KEY
                    CHECK (id ~ '^ctco_[0-9A-HJKMNP-TV-Z]{26}$'),
    case_id         TEXT        NOT NULL REFERENCES app.control_cases(id) ON DELETE CASCADE,
    project_id      TEXT        NOT NULL,
    observed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    detected_by     TEXT        NOT NULL,
    -- Bounded references to the evidence, never the evidence itself: raw failed
    -- rows and provider payloads stay with their Data owner (AC6).
    evidence_refs   JSONB       NOT NULL DEFAULT '[]'::jsonb
                    CHECK (jsonb_typeof(evidence_refs) = 'array'),
    observation     JSONB       NOT NULL DEFAULT '{}'::jsonb
                    CHECK (jsonb_typeof(observation) = 'object'),
    content_hash    TEXT        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT uq_control_case_occurrence UNIQUE (case_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_control_case_occurrences_case
    ON app.control_case_occurrences (case_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS app.control_case_candidates (
    id                  TEXT        PRIMARY KEY
                        CHECK (id ~ '^ctcc_[0-9A-HJKMNP-TV-Z]{26}$'),
    case_id             TEXT        NOT NULL REFERENCES app.control_cases(id) ON DELETE CASCADE,
    project_id          TEXT        NOT NULL,
    candidate_kind      TEXT        NOT NULL
                        CHECK (candidate_kind ~ '^[a-z][a-z0-9_]{1,47}$'),
    -- The Data- or Governance-owned artifact this candidate POINTS AT. Story 49.4
    -- is explicit that `app.mapping_proposals` stays a Data-owned candidate
    -- referenced by a case, and never becomes the case's identity.
    owner_kind          TEXT        NOT NULL CHECK (owner_kind IN ('data', 'governance')),
    owner_object_type   TEXT        NOT NULL,
    owner_object_id     TEXT        NOT NULL,
    owner_version_id    TEXT,
    snapshot            JSONB       NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(snapshot) = 'object'),
    proposed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    content_hash        TEXT        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT uq_control_case_candidate UNIQUE (case_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_control_case_candidates_case
    ON app.control_case_candidates (case_id, proposed_at DESC);

CREATE TABLE IF NOT EXISTS app.control_case_impacts (
    id              TEXT        PRIMARY KEY
                    CHECK (id ~ '^ctci_[0-9A-HJKMNP-TV-Z]{26}$'),
    case_id         TEXT        NOT NULL REFERENCES app.control_cases(id) ON DELETE CASCADE,
    candidate_id    TEXT        REFERENCES app.control_case_candidates(id) ON DELETE CASCADE,
    project_id      TEXT        NOT NULL,
    -- SERVER-derived. An impact a caller supplied describes what the caller
    -- claimed, which is the defect Story 48.3 removed from the money calculators.
    derived_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    affected        JSONB       NOT NULL DEFAULT '{}'::jsonb
                    CHECK (jsonb_typeof(affected) = 'object'),
    coverage        JSONB       NOT NULL DEFAULT '{}'::jsonb
                    CHECK (jsonb_typeof(coverage) = 'object'),
    dependency_fingerprint TEXT  NOT NULL CHECK (dependency_fingerprint ~ '^[0-9a-f]{64}$'),
    content_hash    TEXT        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT uq_control_case_impact UNIQUE (case_id, content_hash)
);

CREATE TABLE IF NOT EXISTS app.control_case_decisions (
    id                  TEXT        PRIMARY KEY
                        CHECK (id ~ '^ctcd_[0-9A-HJKMNP-TV-Z]{26}$'),
    case_id             TEXT        NOT NULL REFERENCES app.control_cases(id) ON DELETE CASCADE,
    project_id          TEXT        NOT NULL,
    decision_kind       TEXT        NOT NULL
                        CHECK (decision_kind ~ '^[a-z][a-z0-9_]{1,47}$'),
    candidate_id        TEXT        REFERENCES app.control_case_candidates(id) ON DELETE SET NULL,
    actor               TEXT        NOT NULL,
    reason              TEXT        NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 600),
    effective_from      DATE        NOT NULL,
    expires_at          DATE,
    -- The exact confirmation, operation and audit references AC2 requires. A
    -- decision that cannot name them is an assertion, not a decision.
    confirmation_id     TEXT,
    operation_id        TEXT,
    audit_ref           TEXT,
    -- What the owner adapter actually reported back, including a failure. A
    -- decision recorded as taken when the owner command failed is worse than no
    -- record: it reads as done.
    owner_outcome       TEXT        NOT NULL DEFAULT 'not_applicable'
                        CHECK (owner_outcome IN ('succeeded', 'failed', 'not_applicable')),
    owner_result        JSONB       NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(owner_result) = 'object'),
    decided_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_control_case_decision_interval
        CHECK (expires_at IS NULL OR expires_at >= effective_from)
);

CREATE INDEX IF NOT EXISTS idx_control_case_decisions_case
    ON app.control_case_decisions (case_id, decided_at DESC);

DO $$
DECLARE
    tbl TEXT;
BEGIN
    FOREACH tbl IN ARRAY ARRAY[
        'control_case_occurrences', 'control_case_candidates',
        'control_case_impacts', 'control_case_decisions'
    ] LOOP
        EXECUTE format(
            'DROP TRIGGER IF EXISTS trg_%1$s_append_only ON app.%1$s;'
            'CREATE TRIGGER trg_%1$s_append_only BEFORE UPDATE OR DELETE ON app.%1$s '
            'FOR EACH ROW EXECUTE FUNCTION app.reject_control_record_mutation();',
            tbl
        );
    END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- 4. DQ Monitor: a stable identity with immutable versions.
--
--    `app.dq_baselines` was keyed on `datastream_id` alone, held one mutable
--    column set, and `dq_monitors._write_dq_baseline` OVERWROTE it after a
--    schema drift was detected -- so the second run after a drift passed, and
--    the check silently agreed with whatever the source now looked like. A
--    baseline change is a governed decision here, which is why the baseline
--    lives inside an immutable version.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.dq_monitors (
    id                          TEXT        PRIMARY KEY
                                CHECK (id ~ '^dqm_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id                      TEXT        NOT NULL
                                REFERENCES app.organizations(id) ON DELETE RESTRICT,
    project_id                  TEXT        NOT NULL
                                REFERENCES app.projects(id) ON DELETE CASCADE,
    name                        TEXT        NOT NULL CHECK (name ~ '^[a-z][a-z0-9_]{0,126}$'),
    label                       TEXT        NOT NULL
                                CHECK (length(btrim(label)) BETWEEN 1 AND 160),
    -- The exact thing watched. `target_kind` is opaque here; the profile
    -- validates that the id resolves for that kind.
    target_kind                 TEXT        NOT NULL
                                CHECK (target_kind IN ('datastream', 'output',
                                                       'semantic_concept', 'semantic_view')),
    target_id                   TEXT        NOT NULL,

    lifecycle_status            TEXT        NOT NULL DEFAULT 'draft'
                                CHECK (lifecycle_status IN ('draft', 'published',
                                                            'paused', 'archived')),
    -- RUNTIME state, deliberately a different column from lifecycle: a published
    -- monitor that has never run is `unavailable`, not `healthy`, and a paused
    -- one is neither.
    runtime_state               TEXT        NOT NULL DEFAULT 'unavailable'
                                CHECK (runtime_state IN ('healthy', 'degraded', 'failing',
                                                         'unavailable', 'not_applicable',
                                                         'paused')),
    current_version_id          TEXT,
    pending_version_id          TEXT,
    last_known_good_version_id  TEXT,
    created_by                  TEXT        NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_dq_monitors_scope UNIQUE (id, project_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_dq_monitors_name
    ON app.dq_monitors (project_id, name)
    WHERE lifecycle_status <> 'archived';

CREATE INDEX IF NOT EXISTS idx_dq_monitors_target
    ON app.dq_monitors (project_id, target_kind, target_id);

CREATE TABLE IF NOT EXISTS app.dq_monitor_versions (
    id                  TEXT        PRIMARY KEY
                        CHECK (id ~ '^dqmv_[0-9A-HJKMNP-TV-Z]{26}$'),
    monitor_id          TEXT        NOT NULL,
    project_id          TEXT        NOT NULL,
    version_number      INTEGER     NOT NULL CHECK (version_number >= 1),
    status              TEXT        NOT NULL
                        CHECK (status IN ('draft', 'published', 'superseded', 'archived')),

    -- The policy this version evaluates, pinned to an exact published Rule Set
    -- version when it uses one. "The latest DQ policy" is not a policy.
    rule_set_id         TEXT,
    rule_set_version_id TEXT REFERENCES app.governance_rule_set_versions (id) ON DELETE RESTRICT,
    check_profile       TEXT        NOT NULL
                        CHECK (check_profile ~ '^[a-z][a-z0-9_]{1,63}$'),
    parameters          JSONB       NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(parameters) = 'object'),
    -- The frozen baseline. Inside the version precisely so advancing it is a new
    -- version rather than an UPDATE nobody can date.
    baseline            JSONB       NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(baseline) = 'object'),
    schedule            JSONB       NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(schedule) = 'object'),
    window_days         INTEGER     NOT NULL DEFAULT 1 CHECK (window_days >= 1),
    severity            TEXT        NOT NULL
                        CHECK (severity IN ('blocking', 'degrading', 'informational')),
    applicability       JSONB       NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(applicability) = 'object'),
    target_selectors    JSONB       NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(target_selectors) = 'object'),
    content_hash        TEXT        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    created_by          TEXT        NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_dq_monitor_version UNIQUE (monitor_id, version_number),
    CONSTRAINT uq_dq_monitor_version_scope UNIQUE (id, monitor_id, project_id),
    CONSTRAINT uq_dq_monitor_version_content UNIQUE (monitor_id, content_hash),
    CONSTRAINT fk_dq_monitor_version_scope
        FOREIGN KEY (monitor_id, project_id)
        REFERENCES app.dq_monitors (id, project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_dq_monitor_versions_head
    ON app.dq_monitor_versions (monitor_id, version_number DESC);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_dq_monitors_current_version'
          AND conrelid = 'app.dq_monitors'::regclass
    ) THEN
        ALTER TABLE app.dq_monitors
            ADD CONSTRAINT fk_dq_monitors_current_version
            FOREIGN KEY (current_version_id, id, project_id)
            REFERENCES app.dq_monitor_versions (id, monitor_id, project_id)
            DEFERRABLE INITIALLY IMMEDIATE;
        ALTER TABLE app.dq_monitors
            ADD CONSTRAINT fk_dq_monitors_pending_version
            FOREIGN KEY (pending_version_id, id, project_id)
            REFERENCES app.dq_monitor_versions (id, monitor_id, project_id)
            DEFERRABLE INITIALLY IMMEDIATE;
        ALTER TABLE app.dq_monitors
            ADD CONSTRAINT fk_dq_monitors_lkg_version
            FOREIGN KEY (last_known_good_version_id, id, project_id)
            REFERENCES app.dq_monitor_versions (id, monitor_id, project_id)
            DEFERRABLE INITIALLY IMMEDIATE;
    END IF;
END $$;

CREATE OR REPLACE FUNCTION app.reject_dq_version_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status IN ('published', 'superseded', 'archived') THEN
            RAISE EXCEPTION 'published DQ monitor versions are immutable'
                USING ERRCODE = '23000';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status IN ('superseded', 'archived') THEN
        RAISE EXCEPTION 'published DQ monitor versions are immutable'
            USING ERRCODE = '23000';
    END IF;
    IF OLD.status = 'published' THEN
        IF NEW.status NOT IN ('superseded', 'archived')
            OR NEW.content_hash <> OLD.content_hash
            OR NEW.baseline::text <> OLD.baseline::text
        THEN
            RAISE EXCEPTION 'published DQ monitor versions are immutable'
                USING ERRCODE = '23000';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_dq_monitor_versions_immutable ON app.dq_monitor_versions;
CREATE TRIGGER trg_dq_monitor_versions_immutable
    BEFORE UPDATE OR DELETE ON app.dq_monitor_versions
    FOR EACH ROW EXECUTE FUNCTION app.reject_dq_version_mutation();

-- ---------------------------------------------------------------------------
-- 5. Evaluations: immutable, and honest about what they could not evaluate.
--
--    `dq_api.py` inferred health from alert firings and pull days, so a monitor
--    that had never run and a monitor that had run and passed were the same
--    thing: no alert. The five outcomes below make them different rows, and the
--    coverage numerator/denominator make "passed on nothing" impossible to
--    render as "passed".
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.dq_evaluations (
    id                      TEXT        PRIMARY KEY
                            CHECK (id ~ '^dqe_[0-9A-HJKMNP-TV-Z]{26}$'),
    project_id              TEXT        NOT NULL REFERENCES app.projects(id) ON DELETE CASCADE,
    monitor_id              TEXT        NOT NULL,
    monitor_version_id      TEXT        NOT NULL
                            REFERENCES app.dq_monitor_versions (id) ON DELETE RESTRICT,

    outcome                 TEXT        NOT NULL
                            CHECK (outcome IN ('pass', 'fail', 'error',
                                               'unverifiable', 'not_applicable')),
    window_start            DATE        NOT NULL,
    window_end              DATE        NOT NULL,
    evaluated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Exact references the evaluation stood on: plan/mapping/publication or
    -- Output snapshot, Concept/View, Rule Set, baseline, compiler versions.
    dependency_refs         JSONB       NOT NULL DEFAULT '{}'::jsonb
                            CHECK (jsonb_typeof(dependency_refs) = 'object'),
    dependency_fingerprint  TEXT        NOT NULL
                            CHECK (dependency_fingerprint ~ '^[0-9a-f]{64}$'),

    total_eligible          INTEGER     NOT NULL DEFAULT 0 CHECK (total_eligible >= 0),
    evaluated_count         INTEGER     NOT NULL DEFAULT 0 CHECK (evaluated_count >= 0),
    passed_count            INTEGER     NOT NULL DEFAULT 0 CHECK (passed_count >= 0),
    failed_count            INTEGER     NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
    unavailable_count       INTEGER     NOT NULL DEFAULT 0 CHECK (unavailable_count >= 0),

    -- Bounded, masked, and NOT the rows themselves.
    observed                JSONB       NOT NULL DEFAULT '{}'::jsonb
                            CHECK (jsonb_typeof(observed) = 'object'),
    evidence_refs           JSONB       NOT NULL DEFAULT '[]'::jsonb
                            CHECK (jsonb_typeof(evidence_refs) = 'array'),
    operation_id            TEXT,
    evaluator_version       TEXT        NOT NULL,
    content_hash            TEXT        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),

    CONSTRAINT ck_dq_evaluations_window CHECK (window_end >= window_start),
    -- Zero eligible members is `not_applicable`. It is structurally impossible to
    -- record a pass over nothing, which is the single most common way a quality
    -- dashboard goes green while measuring air.
    CONSTRAINT ck_dq_evaluations_empty_is_not_a_pass
        CHECK (total_eligible > 0 OR outcome IN ('not_applicable', 'unverifiable', 'error')),
    CONSTRAINT ck_dq_evaluations_counts
        CHECK (evaluated_count <= total_eligible
               AND passed_count + failed_count + unavailable_count <= total_eligible),
    CONSTRAINT uq_dq_evaluation UNIQUE (monitor_version_id, window_start, window_end, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_dq_evaluations_monitor
    ON app.dq_evaluations (project_id, monitor_id, evaluated_at DESC);

DROP TRIGGER IF EXISTS trg_dq_evaluations_append_only ON app.dq_evaluations;
CREATE TRIGGER trg_dq_evaluations_append_only
    BEFORE UPDATE OR DELETE ON app.dq_evaluations
    FOR EACH ROW EXECUTE FUNCTION app.reject_control_record_mutation();

-- ---------------------------------------------------------------------------
-- 6. Issues: stable incidents with append-only workflow history.
--
--    `dq_api.py` parsed the DQ identity out of an alert MESSAGE. An issue that
--    exists only as prose cannot be acknowledged twice, cannot be reopened, and
--    disappears when the message wording changes.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.dq_issues (
    id                  TEXT        PRIMARY KEY
                        CHECK (id ~ '^dqi_[0-9A-HJKMNP-TV-Z]{26}$'),
    project_id          TEXT        NOT NULL REFERENCES app.projects(id) ON DELETE CASCADE,
    monitor_id          TEXT        NOT NULL,
    -- Same deterministic rule as a Control Case: at most one open issue per
    -- (monitor, root cause).
    root_cause_fingerprint TEXT     NOT NULL CHECK (root_cause_fingerprint ~ '^[0-9a-f]{64}$'),
    status              TEXT        NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'acknowledged', 'investigating',
                                          'suppressed', 'resolved', 'closed')),
    severity            TEXT        NOT NULL
                        CHECK (severity IN ('blocking', 'degrading', 'informational')),
    -- A suppression is time-bound by construction; a permanent one is a silent
    -- hole with a friendly name.
    suppressed_until    DATE,
    control_case_id     TEXT REFERENCES app.control_cases(id) ON DELETE SET NULL,
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_dq_issues_suppression
        CHECK (status <> 'suppressed' OR suppressed_until IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_dq_issues_open_root_cause
    ON app.dq_issues (project_id, monitor_id, root_cause_fingerprint)
    WHERE status <> 'closed';

CREATE INDEX IF NOT EXISTS idx_dq_issues_attention
    ON app.dq_issues (project_id, status, severity, last_seen_at DESC);

DROP TRIGGER IF EXISTS trg_dq_issues_updated_at ON app.dq_issues;
CREATE TRIGGER trg_dq_issues_updated_at
    BEFORE UPDATE ON app.dq_issues
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

CREATE TABLE IF NOT EXISTS app.dq_issue_events (
    id              TEXT        PRIMARY KEY
                    CHECK (id ~ '^dqie_[0-9A-HJKMNP-TV-Z]{26}$'),
    issue_id        TEXT        NOT NULL REFERENCES app.dq_issues(id) ON DELETE CASCADE,
    project_id      TEXT        NOT NULL,
    event_kind      TEXT        NOT NULL
                    CHECK (event_kind IN ('observed', 'acknowledged', 'investigating',
                                          'suppressed', 'resolved', 'reopened', 'closed')),
    actor           TEXT        NOT NULL,
    -- Every workflow action records a reason. An acknowledgement with no reason
    -- is a click, and a month later nobody can say why the alarm was silenced.
    reason          TEXT,
    evaluation_id   TEXT REFERENCES app.dq_evaluations(id) ON DELETE SET NULL,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_dq_issue_events_reason
        CHECK (event_kind = 'observed' OR (reason IS NOT NULL AND length(btrim(reason)) > 0))
);

CREATE INDEX IF NOT EXISTS idx_dq_issue_events_issue
    ON app.dq_issue_events (issue_id, occurred_at DESC);

DROP TRIGGER IF EXISTS trg_dq_issue_events_append_only ON app.dq_issue_events;
CREATE TRIGGER trg_dq_issue_events_append_only
    BEFORE UPDATE OR DELETE ON app.dq_issue_events
    FOR EACH ROW EXECUTE FUNCTION app.reject_control_record_mutation();

-- ---------------------------------------------------------------------------
-- 7. The one command family. Same shape as `app.semantic_change_sets` (142) and
--    `app.project_change_sets` (131) on purpose: three change-set tables that
--    behave differently would be three things to learn and three to get wrong.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.controls_change_sets (
    id                      TEXT        PRIMARY KEY
                            CHECK (id ~ '^ccs_[0-9A-HJKMNP-TV-Z]{26}$'),
    project_id              TEXT        NOT NULL REFERENCES app.projects(id) ON DELETE CASCADE,
    object_type             TEXT        NOT NULL
                            CHECK (object_type IN ('control-case', 'dq-monitor', 'rule-set')),
    object_id               TEXT,
    base_version_id         TEXT,
    intent                  JSONB       NOT NULL CHECK (jsonb_typeof(intent) = 'object'),
    diff                    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    impact                  JSONB       NOT NULL DEFAULT '{}'::jsonb,
    dependency_fingerprint  TEXT        CHECK (dependency_fingerprint IS NULL
                                                OR dependency_fingerprint ~ '^[0-9a-f]{64}$'),
    state                   TEXT        NOT NULL DEFAULT 'open'
                            CHECK (state IN ('open', 'prepared', 'confirmed',
                                             'rejected', 'expired')),
    confirmation_token_hash TEXT        CHECK (confirmation_token_hash IS NULL
                                                OR confirmation_token_hash ~ '^[0-9a-f]{64}$'),
    confirmation_used_at    TIMESTAMPTZ,
    expires_at              TIMESTAMPTZ,
    result_version_id       TEXT,
    idempotency_key_hash    TEXT        NOT NULL CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    created_by              TEXT        NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_controls_change_sets_idempotency
        UNIQUE (project_id, idempotency_key_hash),
    -- Single-use: a confirmed change set always carries the moment it was consumed.
    CONSTRAINT ck_controls_change_sets_confirmation
        CHECK (state <> 'confirmed' OR confirmation_used_at IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_controls_change_sets_object
    ON app.controls_change_sets (project_id, object_type, object_id, created_at DESC);

DROP TRIGGER IF EXISTS trg_controls_change_sets_updated_at ON app.controls_change_sets;
CREATE TRIGGER trg_controls_change_sets_updated_at
    BEFORE UPDATE ON app.controls_change_sets
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- ---------------------------------------------------------------------------
-- 8. THE MIGRATION REFUSAL.
--
--    Nothing is migrated because every superseded store is empty (measured
--    above). This block is what happens when that is NOT true: it names the rows
--    and ABORTS, rather than resolving a connector label or a metric name into an
--    id it would have to guess. The Implementation Gate asks for exactly that --
--    "refuse unresolved or ambiguous equal-label mappings instead of guessing" --
--    and a refusal that only exists on an empty database is not a refusal.
--
--    The reconciliation content itself is migrated by the story that owns its
--    profile, not here: 49.4 provides the substrate, and each capability story
--    materializes its own family (48.3 Money/FX/Timezone, 48.4 Tax & Fees).
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    unresolved INTEGER := 0;
    detail     TEXT;
BEGIN
    -- An overlap group addresses its sources by CONNECTOR LABEL
    -- (app.overlap_group_members.connector). A Rule Set version pins exact
    -- Datastream and Semantic Concept versions. There is no total function from
    -- the first to the second: one label can name zero, one or several
    -- Datastreams in a Project, and picking one is picking a meaning.
    SELECT COUNT(*) INTO unresolved FROM app.overlap_groups;
    IF unresolved > 0 THEN
        SELECT string_agg(DISTINCT canonical_name, ', ') INTO detail FROM app.overlap_groups;
        RAISE EXCEPTION
            'Controls & Quality migration refused: % reconciliation overlap group(s) address '
            'their sources by connector label (%). A Rule Set version pins exact Datastream '
            'and Concept versions, and no total function maps a label to one. Re-declare them '
            'through the governed Rule Set lifecycle before applying this migration.',
            unresolved, detail
            USING ERRCODE = '23000';
    END IF;

    -- A DQ baseline is keyed on datastream_id with no monitor, no version and no
    -- date. Adopting it would mean inventing the monitor identity it never had.
    SELECT COUNT(*) INTO unresolved FROM app.dq_baselines;
    IF unresolved > 0 THEN
        RAISE EXCEPTION
            'Controls & Quality migration refused: % mutable DQ baseline(s) exist. They carry '
            'no monitor identity, no version and no decision date, so adopting them would '
            'invent the governed identity they never had. Publish DQ Monitor versions first.',
            unresolved
            USING ERRCODE = '23000';
    END IF;

    RAISE NOTICE 'Controls & Quality migration: 0 rows to migrate; every superseded store is empty.';
END $$;

COMMENT ON TABLE app.control_cases IS
    'Story 49.4: the stable Control Case. One OPEN case per (Project, root_cause_fingerprint) is the documented recurrence rule, enforced by a partial unique index; a later observation appends an occurrence or opens a linked successor, and never edits history.';
COMMENT ON TABLE app.dq_evaluations IS
    'Story 49.4: immutable evaluation records. ck_dq_evaluations_empty_is_not_a_pass makes it structurally impossible to record a pass over zero eligible members -- the most common way a quality dashboard goes green while measuring nothing.';
COMMENT ON TABLE app.rule_set_approvals IS
    'Story 49.4, completing the Story 48.3 Rule Set substrate. An approval is who accepted a version; an exception is which subject is exempt. Different actors, different records.';

COMMIT;
