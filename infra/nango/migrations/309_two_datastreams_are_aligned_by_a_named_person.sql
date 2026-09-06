-- infra/nango/migrations/309_two_datastreams_are_aligned_by_a_named_person.sql
--
-- Story 70.3 -- Analytics Alignment becomes the SEVENTH Project capability, and
-- the two acts a person may take on a row the cascade could not settle get the
-- dated store they never had.
--
-- PART 1 -- THE SEVENTH KEY. Migration 131 wrote five capability keys in three
-- places; migration 243 corrected forward to six. This one does the same for the
-- seventh, under the SAME constraint names PostgreSQL generated for 131's inline
-- CHECKs (read from a live database with pg_get_constraintdef, and re-created
-- verbatim by 243). Guessing those names would drop nothing and leave the old
-- constraint refusing the key beside a new one accepting it.
--
-- The backfill at the end is here for the reason 243 states and that has not
-- changed: NO statement in server/core ever INSERTs into app.project_capabilities.
-- Prepare and confirm both UPDATE, and neither reads the row count. An
-- activation aimed at a row that does not exist updates zero rows, raises
-- nothing, mints its Configuration Version and reports success -- a capability
-- that stays absent while the Change Set says it was turned on.
--
-- `optional`, from the ratified card: Analytics Alignment is off by default
-- (docs/product-architecture/capabilities/analytics-alignment.md, decision 3,
-- "ships as a Project capability of the column-adding kind ..., off by
-- default"). It therefore joins the second member of the pairing CHECK and can
-- never be written `always_present`.
--
-- PART 2 -- THE DECISIONS. The cascade of core/analytics_alignment.py resolves a
-- row by one of four automatic methods, or leaves it `ambiguous` (two candidates
-- and nobody has arbitrated) or `unmatched` (nothing on the other side). Both of
-- those are settled by a PERSON, and a person's act is dated and signed --
-- which no table could hold: the alignment itself is derived at read from the
-- published mapping versions, so there is no row to decorate.
--
-- WHAT THIS TABLE IS NOT: it is not the alignment. It carries no metric, no
-- weight and no amount, and it is absent from server/core/mirror_sync.py's
-- explicit table list, so no dbt model can read it. The three columns the
-- capability adds are derived; only the two human acts are stored.
--
-- APPEND-ONLY? NO, and therefore no `app.rgpd_erasure` hatch of the kind
-- 099_rgpd_erasure_trigger_guards.sql adds, and none of the privilege repair of
-- 277. This table installs no immutability trigger and no append-only journal.
-- What makes the FIRST decision stand is a UNIQUE index plus the writer's
-- ON CONFLICT DO NOTHING -- the exact shape migration 245 chose for
-- app.plan_unmatched_spend_decisions -- and erasure reaches these rows through
-- the ON DELETE CASCADE below, which core/org_purge.py already walks by way of
-- app.projects and app.organizations.
--
-- Schema-Change-Checklist: additive and idempotent. Two CHECK constraints are
-- REPLACED (dropped then re-created wider); every row that satisfied the old
-- ones satisfies the new ones. No migration below this number is edited. This is
-- migration 309.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. The seventh capability key.
-- ---------------------------------------------------------------------------

ALTER TABLE app.project_capabilities
    DROP CONSTRAINT IF EXISTS project_capabilities_capability_key_check;
ALTER TABLE app.project_capabilities
    ADD CONSTRAINT project_capabilities_capability_key_check
    CHECK (capability_key IN (
        'country', 'currency_fx', 'reporting_timezone', 'tax_fees', 'competitors',
        'placement_mapping', 'analytics_alignment'
    ));

-- The pairing CHECK is the one that is easy to miss: it re-enumerates every key
-- beside the availability it is allowed to carry, so a key accepted by the first
-- constraint and absent from this one is refused anyway.
ALTER TABLE app.project_capabilities
    DROP CONSTRAINT IF EXISTS project_capabilities_check1;
ALTER TABLE app.project_capabilities
    ADD CONSTRAINT project_capabilities_check1
    CHECK ((capability_key IN ('currency_fx', 'reporting_timezone')
            AND availability = 'always_present')
        OR (capability_key IN ('country', 'tax_fees', 'competitors',
                               'placement_mapping', 'analytics_alignment')
            AND availability = 'optional'));

CREATE OR REPLACE FUNCTION app.seed_project_capabilities(target_project_id TEXT)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO app.project_capabilities (project_id, capability_key, availability, state)
    VALUES
        (target_project_id, 'country', 'optional', 'disabled'),
        (target_project_id, 'currency_fx', 'always_present', 'draft'),
        (target_project_id, 'reporting_timezone', 'always_present', 'draft'),
        (target_project_id, 'tax_fees', 'optional', 'disabled'),
        (target_project_id, 'competitors', 'optional', 'disabled'),
        (target_project_id, 'placement_mapping', 'optional', 'disabled'),
        (target_project_id, 'analytics_alignment', 'optional', 'disabled')
    ON CONFLICT (project_id, capability_key) DO NOTHING;
END;
$$;

-- The reprise. Idempotent through the ON CONFLICT above, so it adds the missing
-- seventh row to every Project that already carries six.
SELECT app.seed_project_capabilities(id) FROM app.projects;

-- ---------------------------------------------------------------------------
-- 2. The two acts a person may take on one row of one aligned pair.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.analytics_alignment_decisions (
    id                    TEXT        PRIMARY KEY,   -- prefixed ULID: 'aad_<ULID>'
    -- org_id is carried, not derived, for exactly one reason: migration 273's
    -- ratchet arms every org-scoped table from this column, and a table that
    -- omits it leaves the sweep in silence. The policy below is the 32-table
    -- form, word for word.
    org_id                TEXT        NOT NULL
                          REFERENCES app.organizations(id) ON DELETE CASCADE,
    project_id            TEXT        NOT NULL
                          REFERENCES app.projects(id) ON DELETE CASCADE,
    -- The pair is ORDERED, and the order is the one the cascade ran in: the left
    -- Datastream is the one whose rows are being aligned. Swapping the sides is
    -- a different run with a different set of unmatched rows, so it is a
    -- different identity here and not the same decision read backwards.
    left_datastream_id    TEXT        NOT NULL
                          REFERENCES app.datastreams(id) ON DELETE CASCADE,
    right_datastream_id   TEXT        NOT NULL
                          REFERENCES app.datastreams(id) ON DELETE CASCADE,
    -- The EXACT common key version the pair was crossed on. Not the key: a key
    -- whose components changed is a different identity, and a decision taken
    -- under the old one is not a decision about the new one.
    common_key_version_id TEXT        NOT NULL
                          REFERENCES app.mdm_common_key_versions(id) ON DELETE CASCADE,
    left_row_key          TEXT        NOT NULL
                          CHECK (btrim(left_row_key) <> '' AND length(left_row_key) <= 400),
    decision              TEXT        NOT NULL
                          CHECK (decision IN ('arbitrated', 'accepted')),
    right_row_key         TEXT        NULL
                          CHECK (right_row_key IS NULL
                                 OR (btrim(right_row_key) <> ''
                                     AND length(right_row_key) <= 400)),
    reason                TEXT        NULL,
    decided_by            TEXT        NOT NULL CHECK (btrim(decided_by) <> ''),
    decided_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- An arbitration NAMES the row it picked; an acceptance names none, because
    -- it states that nothing on the other side answers this row. The database
    -- refuses both halves of the confusion: an arbitration with no side, and an
    -- acceptance that quietly carries one.
    CONSTRAINT analytics_alignment_decisions_shape_ck CHECK (
        (decision = 'arbitrated' AND right_row_key IS NOT NULL)
     OR (decision = 'accepted'   AND right_row_key IS NULL)
    ),
    -- A Datastream aligned with itself is not a cross.
    CONSTRAINT analytics_alignment_decisions_pair_ck CHECK (
        left_datastream_id <> right_datastream_id
    )
);

-- ONE decision per row of one pair under one key version, and THE FIRST ONE
-- STANDS: `record_alignment_decision` inserts with ON CONFLICT DO NOTHING and
-- re-reads, so a second click -- or a second person -- gets the first author and
-- the first date back instead of overwriting them.
CREATE UNIQUE INDEX IF NOT EXISTS analytics_alignment_decisions_uq_row
    ON app.analytics_alignment_decisions
       (project_id, left_datastream_id, right_datastream_id,
        common_key_version_id, left_row_key);

-- The read path of the cascade: every decision of one pair under one key
-- version, in one index scan, taken beside the derivation it settles.
CREATE INDEX IF NOT EXISTS analytics_alignment_decisions_pair
    ON app.analytics_alignment_decisions
       (project_id, left_datastream_id, right_datastream_id, common_key_version_id);

ALTER TABLE app.analytics_alignment_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.analytics_alignment_decisions FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS analytics_alignment_decisions_epic36
    ON app.analytics_alignment_decisions;
CREATE POLICY analytics_alignment_decisions_epic36 ON app.analytics_alignment_decisions
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id))
    WITH CHECK (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

-- SELECT and INSERT, and deliberately no UPDATE and no DELETE: a dated act is
-- not a mutable field. Withdrawing a decision is a gesture no story has opened,
-- and granting the privilege for it here would let one arrive without one.
GRANT SELECT, INSERT ON app.analytics_alignment_decisions TO connector;

COMMENT ON TABLE app.analytics_alignment_decisions IS
    'Story 70.3: the two acts a person may take on one row of an Analytics Alignment pair '
    'the method cascade could not settle -- ARBITRATED (picked one of the candidates, the '
    'row becomes matched under the human_arbitration method) or ACCEPTED (nothing on the '
    'other side answers this row, the row stays listed with its three added columns null). '
    'Carries no metric, no weight and no amount, and is absent from mirror_sync.py, so no '
    'dbt model can read it. The first decision on a row stands.';
COMMENT ON COLUMN app.analytics_alignment_decisions.common_key_version_id IS
    'The EXACT mdm_common_key_versions row the pair was crossed on. A key whose components '
    'changed is a different identity, so a decision taken under the old version is not a '
    'decision about the new one and never travels forward silently.';
COMMENT ON COLUMN app.analytics_alignment_decisions.right_row_key IS
    'The row the arbitration picked, and NULL on an acceptance. Null is the answer and not '
    'an absence: accepting says nothing on the other side answers this row.';
COMMENT ON COLUMN app.analytics_alignment_decisions.decided_by IS
    'The person who took the act. An anonymous arbitration is indistinguishable from a row '
    'somebody clicked past.';

COMMIT;
