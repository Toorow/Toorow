-- infra/nango/migrations/119_fee_tax_alignment.sql
--
-- Story 41.1 (Epic 41, media cost composition): activation flag + the FeeTaxRule
-- model + the module-owned source_type home + the flattening views the governed
-- warehouse mirror reads.
--
-- EDITED IN PLACE BY STORY 41.5 (2026-07-27, orchestrator-authorised). This file is
-- committed but APPLIED TO NO DATABASE, so the vocabulary fix is free now and
-- expensive forever after. Two changes, both inside app.fee_tax_rules:
--   * the `form` CHECK gains 'PER_TRANSACTION' (a gateway fee of
--     amount_micros x transaction_count), with FLAT-shaped payload coherence in
--     ck_fee_tax_rules_form_payload. FLAT keeps exactly ONE meaning platform-wide:
--     "this amount, once for the row".
--   * two derived routed-pair guards, ck_fee_tax_rules_category_form and
--     ck_fee_tax_rules_form_base_target, close review finding F2 -- see the long
--     comment above them. NO new migration number was consumed; there is no 120.
--   Regenerate the manifest after editing this file:
--     python scripts/check_migration_catalog.py infra/nango/migrations --write-manifest
--
-- What this adds (all additive, all idempotent):
--   1. app.project_preferences.fee_tax_alignment_enabled BOOLEAN NOT NULL DEFAULT
--      FALSE -- the per-project switch (C.1). Twin of geographic_mode (057).
--      OFF BY DEFAULT is the headline invariant (E41-NFR01): every pre-existing
--      row reads FALSE the second this migration commits, and a project with no
--      preferences row reads OFF too.
--   2. app.fee_tax_rules -- the declared fee/tax ladder as governed DATA (C.2).
--      The ladder is a sequence of rows, not code: a new fee category is a row.
--   3. app.datastream_source_types -- the AD-8 source_type, module-owned (C.4).
--      app.datastreams is NOT altered.
--   4. Four read-only projections consumed by server/core/mirror_sync.py:
--        app.fee_tax_rules_dim_v        (id, project_id, scope_kind, scope_ref,
--                                        category, form, rate, amount_micros,
--                                        cpm_micros, currency, base_target,
--                                        cascade_phase, sequence_order,
--                                        effective_from, effective_to, status,
--                                        origin, dedup_hash, label, created_by,
--                                        created_at, updated_at)
--                                       -- SCALARS ONLY: no conditions, no tiers,
--                                          no source_type_scope
--        app.fee_tax_rule_conditions_v  (rule_id, condition_key, condition_value)
--        app.fee_tax_rule_tiers_v       (rule_id, tier_index, threshold_micros,
--                                        rate, mode)
--        app.datastreams_dim_v          (project_id, datastream_id, connector,
--                                        data_role, source_kind)
--      C.8 decision 1: no raw JSONB and no array crosses the mirror (psycopg ->
--      pandas -> DuckDB lands an unpredictable type). Flattening happens HERE, in
--      Postgres, where the JSONB semantics are exact -- which deletes every JSON
--      macro from the 41.3 dbt engine.
--
-- What this deliberately does NOT do:
--   * NO INSERT of any kind. No country, no rate, no default rule, no preference
--     row (E41-NFR04 / C.3). Country tax defaults are a dbt SEED and that seed is
--     Story 41.2's. Seeding a preference row from a migration would also trip
--     tests/conformance/test_no_geographic_hardcode.py, which scans every
--     migration file for that exact statement.
--   * NO immutability trigger (unlike 097 L98-144): fee/tax rules are EDITABLE by
--     design (status machine + rate overrides). Because there is no append-only
--     DELETE guard, app.fee_tax_rules does NOT join the migration-099 RGPD
--     erasure allowlist -- that list is exactly the tables whose DELETE trigger
--     would block an erasure. server/core/org_purge.py discovers the tenant tree
--     from the FK graph, so the ON DELETE CASCADE below is picked up with no
--     registration.
--   * NO UNIQUE (project_id, cascade_phase, sequence_order). C.8 decision 3:
--     every rule inside a cascade phase reads the subtotal AS AT PHASE ENTRY, so
--     intra-phase rules do not compound and sequence_order is a display/override
--     key. Two rules may legitimately share a slot.
--   * app.datastreams and app.project_modules are untouched.
--
-- Numbering verified: 001..118 present on disk with no gaps; 119 is the first
-- free identifier. Regenerate the manifest with
--   python scripts/check_migration_catalog.py infra/nango/migrations --write-manifest
--
-- Apply with (human-gated on Jean's authorization):
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector \
--     -f /migrations/119_fee_tax_alignment.sql

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Activation (C.1, E41-FR01 / E41-AD1)
--
-- Additive column only. The 102 constraint chk_project_preferences_geographic_aggregate
-- is NOT touched: this column is orthogonal to the geographic posture, and the
-- application upsert (core/fee_tax_rules.py::set_fee_tax_alignment) names ONLY
-- this column so a configured local_markets posture is never clobbered.
-- ---------------------------------------------------------------------------

ALTER TABLE app.project_preferences
    ADD COLUMN IF NOT EXISTS fee_tax_alignment_enabled BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN app.project_preferences.fee_tax_alignment_enabled IS
    'Story 41.1: per-project switch for the fee and tax alignment module. FALSE '
    '(or an absent preferences row) means OFF: every Epic 41 surface is inert. '
    'app.project_modules is NOT used for alignment modules.';

-- ---------------------------------------------------------------------------
-- 2a. JSONB shape validators (review finding F4).
--
-- A CHECK constraint cannot contain a subquery, so the per-element invariants
-- live in IMMUTABLE helpers -- the same device migration 102 uses for
-- app.geographic_markets_valid.
--
-- Why these are NOT optional. The flattening views in section 4 are what the
-- warehouse reads, and a single malformed row would make a view RAISE for EVERY
-- project's rules, not just its own: jsonb_array_elements_text() errors on a
-- non-array, and one fetch error aborts the ENTIRE nightly mirror sync. The
-- column-level `jsonb_typeof(conditions) = 'object'` alone let
-- '{"country": "FR"}' (a scalar value, not an array) through. Rejecting the row
-- at write time is the only fix that neither drops data nor breaks the view; a
-- defensive WHERE in the view would silently hide the rule instead.
--
-- These guard the case where a writer BYPASSES core/fee_tax_rules.py, which is
-- exactly the threat model the "defence in depth" comment below claims to cover.
-- ---------------------------------------------------------------------------

-- Every value of the conditions object must be a JSON array. '{}' stays valid
-- (it means "matches every row").
CREATE OR REPLACE FUNCTION app.fee_tax_conditions_valid(conditions JSONB)
RETURNS BOOLEAN
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN conditions IS NULL THEN FALSE
        WHEN jsonb_typeof(conditions) <> 'object' THEN FALSE
        ELSE NOT EXISTS (
            SELECT 1
            FROM jsonb_each(conditions) AS entry
            WHERE jsonb_typeof(entry.value) <> 'array'
        )
    END;
$$;

-- Every tier band must be an object whose threshold_micros and rate can be cast
-- by app.fee_tax_rule_tiers_v without raising, and whose magnitudes fit the
-- target SQL types (BIGINT / NUMERIC(12,6)) so the cast cannot overflow either.
-- CASE (not AND) so the jsonb_array_elements subquery is never reached for a
-- non-array `bands` -- evaluation order inside a CASE is guaranteed.
CREATE OR REPLACE FUNCTION app.fee_tax_tier_bands_valid(tiers JSONB)
RETURNS BOOLEAN
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN tiers IS NULL THEN TRUE
        WHEN jsonb_typeof(tiers) <> 'object' THEN FALSE
        WHEN jsonb_typeof(tiers->'bands') <> 'array' THEN FALSE
        ELSE NOT EXISTS (
            SELECT 1
            FROM jsonb_array_elements(tiers->'bands') AS band
            WHERE jsonb_typeof(band) <> 'object'
               OR COALESCE(band->>'threshold_micros', '') !~ '^-?[0-9]{1,18}$'
               OR COALESCE(band->>'rate', '') !~ '^-?[0-9]{1,6}(\.[0-9]{1,6})?$'
        )
    END;
$$;

-- ---------------------------------------------------------------------------
-- 2b. app.fee_tax_rules -- the declared ladder (C.2, E41-FR02 / E41-AD2)
--
-- Money is exact: rate is NUMERIC(12,6) (0.030000 = 3 percent) and amounts are
-- integer micros. Never REAL / DOUBLE PRECISION (E41-NFR02).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.fee_tax_rules (
    id                 TEXT          NOT NULL PRIMARY KEY,          -- ftr_<ULID>
    project_id         TEXT          NOT NULL
                           REFERENCES app.projects(id) ON DELETE CASCADE,
    scope_kind         TEXT          NOT NULL
                           CHECK (scope_kind IN ('project', 'plan_version', 'datastream')),
    scope_ref          TEXT          NULL,        -- plan version id | datastream id
    category           TEXT          NOT NULL
                           CHECK (category IN ('PLATFORM_FEE', 'VERIFICATION',
                                               'REGULATORY_TAX', 'WHT_GROSS_UP',
                                               'AGENCY_FEE', 'SALES_TAX',
                                               'PAYMENT_FEE')),
    -- PER_TRANSACTION added by Story 41.5 (orchestrator ruling 3, 2026-07-27): a
    -- per-transaction gateway fee is `amount_micros x transaction_count`, which is a
    -- DIFFERENT behaviour from FLAT ("this amount, once for the row"). Overloading
    -- FLAT by (category, base_target) would make a money semantic depend on context a
    -- reader has to reconstruct. It carries the SAME payload shape as FLAT
    -- (amount_micros + currency) because it is an absolute amount too -- see
    -- ck_fee_tax_rules_form_payload below.
    form               TEXT          NOT NULL
                           CHECK (form IN ('PERCENTAGE', 'CPM', 'FLAT',
                                           'SPEND_TIERS', 'GROSS_UP',
                                           'PER_TRANSACTION')),
    rate               NUMERIC(12,6) NULL CHECK (rate IS NULL OR rate >= 0),
    amount_micros      BIGINT        NULL,
    cpm_micros         BIGINT        NULL,
    -- {"mode":"cliff"|"marginal","bands":[{threshold_micros, rate}, ...]}
    -- C.8 decision 4: the evaluation mode is RULE DATA, not a platform choice.
    tiers              JSONB         NULL,
    currency           TEXT          NULL CHECK (currency IS NULL OR currency ~ '^[A-Z]{3}$'),
    base_target        TEXT          NOT NULL
                           CHECK (base_target IN ('NET_MEDIA', 'RUNNING_SUBTOTAL',
                                                  'MEASURED_IMPRESSIONS', 'NET_REVENUE',
                                                  'GROSS_REVENUE')),
    cascade_phase      SMALLINT      NOT NULL CHECK (cascade_phase BETWEEN 1 AND 6),
    sequence_order     INTEGER       NOT NULL CHECK (sequence_order >= 0),
    -- Row-level match (E41-AD9). Recognised keys: country, market, placement_type,
    -- connector, tax_code. The JSONB stays OPEN, but an unrecognised key is
    -- UNRESOLVED at cascade time, never silently ignored.
    conditions         JSONB         NOT NULL DEFAULT '{}'::jsonb,
    source_type_scope  TEXT[]        NOT NULL DEFAULT ARRAY[]::TEXT[],
    effective_from     DATE          NOT NULL,
    effective_to       DATE          NULL,
    status             TEXT          NOT NULL DEFAULT 'proposed'
                           CHECK (status IN ('proposed', 'confirmed', 'disabled')),
    origin             TEXT          NOT NULL
                           CHECK (origin IN ('auto_country', 'operator', 'media_plan', 'llm')),
    -- Idempotency slot for auto-population (Story 41.2 computes it for
    -- origin='auto_country'). operator / llm / media_plan rules leave it NULL and
    -- are unconstrained by the partial unique index below.
    --
    -- Named dedup_HASH, not dedup_key, for two reasons. It is what the value
    -- actually is: a content hash of the facts that make the rule unique, not a key
    -- in any lookup or secret sense. And operations._is_secret_key treats any
    -- identifier ending in `_key` as secret-like unless it ends in _id/_ref/_hash,
    -- so a column called dedup_key could never appear in an audited request_payload
    -- -- every auto-populated rule would have failed at write time. Ending in
    -- _hash keeps the field auditable instead of tunnelling under that invariant.
    dedup_hash         TEXT          NULL CHECK (dedup_hash IS NULL OR btrim(dedup_hash) <> ''),
    label              TEXT          NOT NULL DEFAULT '',
    created_by         TEXT          NOT NULL,
    created_at         TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    -- House-style prefixed ULID (Crockford base32, 26 chars) -- cf. fst_/mdm_/dse_.
    CONSTRAINT ck_fee_tax_rules_id
        CHECK (id ~ '^ftr_[0-9A-HJKMNP-TV-Z]{26}$'),
    -- scope_ref is NULL iff the rule is project-wide (C.2).
    CONSTRAINT ck_fee_tax_rules_scope_ref
        CHECK ((scope_kind = 'project' AND scope_ref IS NULL)
            OR (scope_kind <> 'project' AND scope_ref IS NOT NULL)),
    -- Defence in depth over validate_rule(): a stored rule must be EXECUTABLE by
    -- the 41.3 cascade or it must not exist (C.2).
    CONSTRAINT ck_fee_tax_rules_form_payload
        CHECK (
            (form IN ('PERCENTAGE', 'GROSS_UP') AND rate IS NOT NULL)
         OR (form = 'FLAT' AND amount_micros IS NOT NULL AND currency IS NOT NULL)
            -- Story 41.5: FLAT-shaped payload. PER_TRANSACTION is an absolute amount
            -- charged once per transaction, so it needs the amount AND the currency
            -- it is denominated in -- 41.5 REFUSES a foreign-currency amount rather
            -- than converting it (Epic 39.10: FX is applied once, at read).
         OR (form = 'PER_TRANSACTION' AND amount_micros IS NOT NULL AND currency IS NOT NULL)
         OR (form = 'CPM'  AND cpm_micros    IS NOT NULL AND currency IS NOT NULL)
         OR (form = 'SPEND_TIERS' AND tiers IS NOT NULL
             AND jsonb_typeof(tiers) = 'object'
             AND jsonb_typeof(tiers->'bands') = 'array'
             AND jsonb_array_length(tiers->'bands') > 0
             AND COALESCE(tiers->>'mode', 'cliff') IN ('cliff', 'marginal'))
        ),
    -- A WHT gross-up rate of 1 divides by zero in net/(1-rate): refuse it at
    -- declaration rather than discovering it in the cascade.
    CONSTRAINT ck_fee_tax_rules_gross_up_rate
        CHECK (form <> 'GROSS_UP' OR (rate IS NOT NULL AND rate >= 0 AND rate < 1)),
    CONSTRAINT ck_fee_tax_rules_effective_window
        CHECK (effective_to IS NULL OR effective_to >= effective_from),
    -- F4: every conditions value is a JSON array, so app.fee_tax_rule_conditions_v
    -- can never raise on jsonb_array_elements_text.
    CONSTRAINT ck_fee_tax_rules_conditions_shape
        CHECK (app.fee_tax_conditions_valid(conditions)),
    -- F4: tiers belongs to SPEND_TIERS and nowhere else. Without this, a
    -- PERCENTAGE row carrying tiers = '{"bands": 5}' passes the form_payload
    -- CHECK (via its PERCENTAGE branch) and then breaks
    -- app.fee_tax_rule_tiers_v for every project.
    CONSTRAINT ck_fee_tax_rules_tiers_form
        CHECK (form = 'SPEND_TIERS' OR tiers IS NULL),
    -- F4: each band casts cleanly to (BIGINT, NUMERIC(12,6)) in the tiers view.
    CONSTRAINT ck_fee_tax_rules_tier_bands
        CHECK (app.fee_tax_tier_bands_valid(tiers)),

    -- =====================================================================
    -- FAIL-CLOSED ROUTING GUARDS (Story 41.5 + review finding F2, approved
    -- 2026-07-27). Read this before touching either of the two CHECKs below.
    --
    -- WHY THESE EXIST. The cascade's phase CTEs in
    -- dbt/models/marts/fee_tax_ladder_daily.sql route a FIXED set of (category, form)
    -- pairs and fall through to `ELSE 0` for everything else -- WITHOUT refusing the
    -- rule, without raising a gap, and without removing it from `fired` /
    -- `applied_rule_ids`. GROSS_UP is routed in phase 4 (WHT_GROSS_UP) and NOWHERE
    -- ELSE; CPM is routed by no cost phase at all. So a pair that is merely plausible
    -- -- (AGENCY_FEE, GROSS_UP), or CPM over NET_MEDIA -- passes every other
    -- constraint here, reaches the cascade, contributes EXACTLY ZERO, and leaves the
    -- row reporting itself COMPLETE. An invoice understated by the whole of the
    -- missing component, behind a green flag, with no gap and no log line. That is
    -- the precise failure mode this epic exists to prevent (E41-NFR02), and Story
    -- 41.8 lets a governed LLM create rules, so it is reachable without hand-written
    -- SQL. Constraining declarations to the pairs an evaluator ACTUALLY IMPLEMENTS
    -- makes the wrong state unrepresentable rather than merely unlikely.
    --
    -- BEFORE YOU WIDEN EITHER CONSTRAINT: add the routing to the phase CTEs of
    -- fee_tax_ladder_daily.sql FIRST (or to the evaluator that owns the new pair),
    -- and only then relax the list here. Widening one of these on its own does not
    -- enable a feature -- it re-opens the silent-zero hole. If you are here because a
    -- legitimate combination needs to exist, the work is in the evaluator, not in
    -- this line.
    --
    -- The allowed set is DERIVED from the engine, not from judgement: see the
    -- derivation table in
    -- _bmad-output/implementation-artifacts/41-5-revenue-tax-symmetry-ht-ttc.md
    -- section B.4ter, which cites the phase CTE line numbers each pair comes from,
    -- and fee_tax_ladder_daily.sql's own positive `form_unroutable` enumeration,
    -- which is the same table written as a guard. The DB refuses the pair; if one
    -- ever slips through (a mirror-lag row, a pre-constraint row), the engine gaps
    -- rather than lies. The two halves are defence in depth BY DESIGN.
    -- =====================================================================

    -- CHECK A -- (category, form). Derived from fee_tax_ladder_daily.sql:
    --   rules_ladder admits PLATFORM_FEE / REGULATORY_TAX / WHT_GROSS_UP /
    --   AGENCY_FEE / SALES_TAX, and `form_unroutable` routes WHT_GROSS_UP ->
    --   {GROSS_UP, PERCENTAGE, FLAT, SPEND_TIERS} and every other admitted category
    --   -> {PERCENTAGE, FLAT, SPEND_TIERS}. VERIFICATION -> CPM is Story 41.4's
    --   overlay (E41-FR04: the fee "arrives in CPM over measured impressions");
    --   PAYMENT_FEE -> {PERCENTAGE, FLAT, PER_TRANSACTION} is Story 41.5's revenue
    --   overlay.
    --
    -- This also SUBSUMES the PER_TRANSACTION scope guard: the value appears in the
    -- PAYMENT_FEE branch and nowhere else, so a per-transaction PLATFORM_FEE (which
    -- the cost cascade would silently zero) is unrepresentable. That invariant lives
    -- here rather than in a separate named constraint on purpose -- one table of
    -- routed pairs, not a growing pile of one-off guards.
    --
    -- STANDING COUPLING: if a future wave adds a second VERIFICATION form (a flat
    -- retainer, say), this CHECK, server/core/fee_tax_rules.py::_CATEGORY_FORMS AND
    -- the evaluator's routing widen in the SAME wave. Do not widen it speculatively
    -- for a form nobody has shipped: that reopens the silent-zero hole for a guess.
    CONSTRAINT ck_fee_tax_rules_category_form
        CHECK (
            (category = 'PLATFORM_FEE'   AND form IN ('PERCENTAGE', 'FLAT', 'SPEND_TIERS'))
         OR (category = 'REGULATORY_TAX' AND form IN ('PERCENTAGE', 'FLAT', 'SPEND_TIERS'))
         OR (category = 'WHT_GROSS_UP'   AND form IN ('GROSS_UP', 'PERCENTAGE', 'FLAT', 'SPEND_TIERS'))
         OR (category = 'AGENCY_FEE'     AND form IN ('PERCENTAGE', 'FLAT', 'SPEND_TIERS'))
         OR (category = 'SALES_TAX'      AND form IN ('PERCENTAGE', 'FLAT', 'SPEND_TIERS'))
         OR (category = 'VERIFICATION'   AND form = 'CPM')
         OR (category = 'PAYMENT_FEE'    AND form IN ('PERCENTAGE', 'FLAT', 'PER_TRANSACTION'))
        ),

    -- CHECK B -- (form, base_target). Same reason, same instruction: widen the
    -- evaluator FIRST. Each line is derived, not assumed:
    --   * CPM -> MEASURED_IMPRESSIONS ONLY. Nothing else forces it today, so a CPM
    --     rule over NET_MEDIA is routed by no phase at all and verification cost
    --     silently vanishes (F2's second instance).
    --   * PER_TRANSACTION -> GROSS_REVENUE. Its base is the transaction count of a
    --     revenue row; there is no cost-side evaluator for it.
    --   * GROSS_UP -> {NET_MEDIA, RUNNING_SUBTOTAL}, because phase 4's `phase_base`
    --     resolves to one or the other and to nothing else.
    --   * SPEND_TIERS -> {NET_MEDIA, RUNNING_SUBTOTAL}, because the cumulative base
    --     IS net media (`tier_rules` reads net_media_micros). A tiered rule on a
    --     revenue base has no evaluator anywhere.
    --   * PERCENTAGE / FLAT may not target MEASURED_IMPRESSIONS: a percentage of an
    --     impression COUNT is not money, and no evaluator computes one.
    CONSTRAINT ck_fee_tax_rules_form_base_target
        CHECK (
            (form = 'CPM'             AND base_target = 'MEASURED_IMPRESSIONS')
         OR (form = 'PER_TRANSACTION' AND base_target = 'GROSS_REVENUE')
         OR (form = 'GROSS_UP'        AND base_target IN ('NET_MEDIA', 'RUNNING_SUBTOTAL'))
         OR (form = 'SPEND_TIERS'     AND base_target IN ('NET_MEDIA', 'RUNNING_SUBTOTAL'))
         OR (form IN ('PERCENTAGE', 'FLAT')
             AND base_target IN ('NET_MEDIA', 'RUNNING_SUBTOTAL',
                                 'NET_REVENUE', 'GROSS_REVENUE'))
        )
);

COMMENT ON TABLE app.fee_tax_rules IS
    'Story 41.1: the fee and tax ladder as governed DATA (one row per declared '
    'rule). Editable by design (status machine + rate overrides), hence no '
    'append-only guard and no migration-099 allowlist entry. Only a rule whose '
    'status is confirmed is ever meant to enter the 41.3 cascade. '
    'Story 41.5 added the PER_TRANSACTION form and the two routed-pair guards '
    'ck_fee_tax_rules_category_form / ck_fee_tax_rules_form_base_target: a '
    '(category, form) or (form, base_target) pair no evaluator routes is '
    'UNREPRESENTABLE, because such a rule would contribute exactly 0 micros while '
    'the row reported itself complete. Widening either guard requires adding the '
    'routing to the evaluator FIRST.';

COMMENT ON COLUMN app.fee_tax_rules.form IS
    'How the component is computed. PERCENTAGE (rate x base) | CPM (cpm_micros per '
    '1000 measured impressions, Story 41.4) | FLAT (this amount, ONCE for the row, '
    'then allocated pro-rata across the day) | SPEND_TIERS (banded rate over '
    'cumulative net media) | GROSS_UP (base/(1-rate), withholding only) | '
    'PER_TRANSACTION (amount_micros x transaction_count, Story 41.5 payment '
    'gateways). FLAT and PER_TRANSACTION are DELIBERATELY two forms: FLAT means '
    '"once", PER_TRANSACTION means "per transaction", and a per-transaction fee '
    'billed once on a day of 4 000 orders is off by 4 000x -- silently.';

-- Ordered reads for the cascade. DELIBERATELY NOT UNIQUE (C.8 decision 3).
CREATE INDEX IF NOT EXISTS ix_fee_tax_rules_project_cascade
    ON app.fee_tax_rules (project_id, cascade_phase, sequence_order);

-- The cascade reads confirmed rules only.
CREATE INDEX IF NOT EXISTS ix_fee_tax_rules_confirmed
    ON app.fee_tax_rules (project_id, cascade_phase, sequence_order)
    WHERE status = 'confirmed';

CREATE INDEX IF NOT EXISTS ix_fee_tax_rules_scope
    ON app.fee_tax_rules (project_id, scope_kind, scope_ref);

-- Re-running 41.2's auto-population cannot duplicate a rule. PARTIAL on purpose:
-- the many operator / llm rows with dedup_hash IS NULL stay unconstrained (a plain
-- UNIQUE would also allow them -- NULLs never collide -- but the partial index
-- stays smaller and states the intent).
CREATE UNIQUE INDEX IF NOT EXISTS uq_fee_tax_rules_dedup
    ON app.fee_tax_rules (project_id, dedup_hash)
    WHERE dedup_hash IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 3. app.datastream_source_types -- module-owned source_type (C.4, E41-AD8)
--
-- app.datastreams is NOT altered: this is a COMPANION table. UNKNOWN is in the
-- vocabulary on purpose -- the 41.2 resolution ladder (declaration -> derivation
-- -> UNKNOWN) must end in a TYPED unknown that is excluded from the cost cascade,
-- never silently treated as PAID_MEDIA.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.datastream_source_types (
    datastream_id TEXT        NOT NULL PRIMARY KEY
                      REFERENCES app.datastreams(id) ON DELETE CASCADE,
    source_type   TEXT        NOT NULL
                      CHECK (source_type IN ('PAID_MEDIA', 'LEAD_GEN_MEDIA',
                                             'DIRECT_SERVICE_COST', 'COMMERCE_REVENUE',
                                             'ORGANIC_ANALYTICS', 'UNKNOWN')),
    declared_by   TEXT        NOT NULL,
    declared_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE app.datastream_source_types IS
    'Story 41.1 (C.4): module-owned AD-8 source_type for a datastream. One row '
    'per datastream (PK). app.datastreams is not altered. The resolution ladder '
    'that populates this table is Story 41.2.';

-- ---------------------------------------------------------------------------
-- 4. Flattening / projection views read by server/core/mirror_sync.py
--
-- CREATE OR REPLACE so 119 stays replayable. These are read-only projections:
-- no INSTEAD OF trigger, nothing writable.
-- ---------------------------------------------------------------------------

-- SCALARS ONLY. This is what the mirror's `fee_tax_rules` entry reads: no
-- conditions, no tiers, and no source_type_scope (C.8 decision 1 -- zero JSONB and
-- zero arrays cross the mirror). Keeping the column list in the view rather than
-- inline in mirror_sync.py means a future column lands in ONE place.
-- Frozen contract: 22 columns, all scalar.
CREATE OR REPLACE VIEW app.fee_tax_rules_dim_v AS
SELECT id,
       project_id,
       scope_kind,
       scope_ref,
       category,
       form,
       rate,
       amount_micros,
       cpm_micros,
       currency,
       base_target,
       cascade_phase,
       sequence_order,
       effective_from,
       effective_to,
       status,
       origin,
       dedup_hash,
       label,
       created_by,
       created_at,
       updated_at
FROM app.fee_tax_rules;

-- Frozen contract: (rule_id, condition_key, condition_value).
--
-- One row per (rule, condition key, condition value). CROSS JOIN LATERAL (not a
-- plain comma join in the SELECT list) so a rule with conditions = '{}' yields NO
-- row at all instead of a phantom NULL row: "{} matches everything" must never
-- become a condition to satisfy.
--
-- source_type_scope (TEXT[] on the base table) rides HERE as rows with
-- condition_key = 'source_type', one row per array element -- so the warehouse has
-- ONE uniform matching mechanism for every condition, arrays included, and no array
-- ever crosses the mirror. An EMPTY source_type_scope yields no rows, which reads
-- exactly like an empty `conditions` object: "applies to everything".
-- No key collision is possible: 'source_type' is deliberately NOT a member of the
-- declarable conditions vocabulary (core/fee_tax_rules.py::CONDITION_KEYS), so a
-- rule can never declare conditions->'source_type' itself.
CREATE OR REPLACE VIEW app.fee_tax_rule_conditions_v AS
SELECT r.id      AS rule_id,
       cond.key  AS condition_key,
       val.value AS condition_value
FROM app.fee_tax_rules r
CROSS JOIN LATERAL jsonb_each(r.conditions)              AS cond(key, value)
CROSS JOIN LATERAL jsonb_array_elements_text(cond.value) AS val(value)
UNION ALL
SELECT r.id                AS rule_id,
       'source_type'::TEXT AS condition_key,
       scope.value         AS condition_value
FROM app.fee_tax_rules r
CROSS JOIN LATERAL unnest(r.source_type_scope) AS scope(value);

-- Frozen contract: (rule_id, tier_index, threshold_micros, rate, mode).
--
-- One row per tier band, ordinality-ordered. `mode` is DENORMALISED onto every band
-- row (it lives on the parent object) so the 41.3 dbt engine can branch cliff vs
-- marginal without re-reading JSON.
-- tier_index is 0-BASED (WITH ORDINALITY is 1-based): 41.3 joins on it.
CREATE OR REPLACE VIEW app.fee_tax_rule_tiers_v AS
SELECT r.id                                      AS rule_id,
       band.ord::INTEGER - 1                     AS tier_index,
       (band.value->>'threshold_micros')::BIGINT AS threshold_micros,
       (band.value->>'rate')::NUMERIC(12,6)      AS rate,
       COALESCE(r.tiers->>'mode', 'cliff')       AS mode
FROM app.fee_tax_rules r
CROSS JOIN LATERAL jsonb_array_elements(r.tiers->'bands') WITH ORDINALITY AS band(value, ord)
WHERE r.tiers IS NOT NULL;

-- Frozen contract: (project_id, datastream_id, connector, data_role, source_kind).
--
-- Minimal datastream projection for the mirror (feeds 41.2's source_type resolution
-- and 41.3's datastream scoping). Same minimal-projection discipline as
-- connection_ref_dim (AD-3): no token, no connection_ref_id, and NO config JSONB
-- (which would reintroduce exactly the risk decision 1 removes).
--
-- `connector` is app.datastreams.module_name ALIASED: everything in the warehouse
-- joins on fact_daily_kpi.connector, so the dim speaks the warehouse's vocabulary,
-- not the control plane's.
-- module_name is NULLABLE since migration 030 (it is NULL for managed_feed and
-- external_bq datastreams). Such a row IS STILL MIRRORED, with connector = NULL: a
-- downstream join on connector simply does not match. It is deliberately NOT
-- filtered out -- an invisible datastream is a silent gap, and 41.2's derivation
-- ladder must see the row in order to resolve it to a typed UNKNOWN.
-- archived_at IS NULL: an archived datastream has no live rules to scope.
CREATE OR REPLACE VIEW app.datastreams_dim_v AS
SELECT project_id,
       id          AS datastream_id,
       module_name AS connector,
       data_role,
       source_kind
FROM app.datastreams
WHERE archived_at IS NULL;

-- NOTE on the fifth mirror entry, datastream_country_binding_dim: it is NOT a
-- view. app.market_bindings (migration 104) is NOT applied, so a view over it
-- could not be created here -- and a view created against an "absent" branch
-- would stay empty FOREVER even after 104 lands, because a view is frozen at
-- creation. mirror_sync.py therefore resolves that one projection at CALL time
-- behind a to_regclass guard, which self-heals the day 104 is applied.

COMMIT;
