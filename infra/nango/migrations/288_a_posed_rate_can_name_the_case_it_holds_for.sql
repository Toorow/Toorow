-- 288 -- a posed rate can name the CASE it holds for, and two of them can coexist
--
-- RATIFIED 2026-08-17 (amendment at the end of
-- docs/product-architecture/alignment-register.md, question 2 of the audit):
-- a user sets the conversion rate "as a value in the table -- a fixed rate per
-- currency pair -- OR as a simple conditional rule (a rate that applies when a
-- named condition holds, in the spirit of a spreadsheet `IF`)".
--
-- Migration 279 delivered the first half: `fixed` became a method of its own, so a
-- number a person typed stopped borrowing `direct`. This migration delivers the
-- second half, and it has to move three things that the first half left standing.
--
-- ---------------------------------------------------------------------------
-- ONE. A CONDITION, IN THE VOCABULARY THAT ALREADY EXISTS.
-- ---------------------------------------------------------------------------
-- `conditions` is the SAME shape `app.fee_tax_rules.conditions` has carried since
-- migration 119: a JSON object mapping a condition key to a non-empty list of
-- admissible values, where `{}` means "holds for every row". It is deliberately not
-- a second vocabulary. The keys FX admits are the subset of `CONDITION_KEYS`
-- (core.fee_tax_rules) that can qualify a RATE -- `connector`, `country`, `market`
-- -- plus `datastream`, which fee/tax carries as a `scope_kind` column because it
-- HAS one and this table does not. `placement_type` and `tax_code` are refused by
-- name at the door: they qualify a fee, not a rate.
--
-- The closed set is enforced in `core.fx_fixed_rates`, not by a CHECK here, for the
-- reason migration 119 gives for the same choice: an unrecognised key must produce a
-- NAMED refusal a caller can act on ("an unknown key is UNRESOLVED, never ignored"),
-- and a constraint violation is not that sentence.
--
-- THE PERIOD IS NOT A CONDITION KEY. A posed rate already holds for a window, and
-- that window has been required since 8748e279 -- expressing it a second time as a
-- condition would give the period two authorities that could disagree. It gets real
-- columns instead (below), because a window resolution cannot read is a decoration.
--
-- ---------------------------------------------------------------------------
-- TWO. THE UNIQUENESS THAT MADE A CONDITIONAL RATE UNSTORABLE.
-- ---------------------------------------------------------------------------
-- Migration 144 wrote:
--
--     UNIQUE (rate_set_version_id, base_currency, quote_currency, effective_date)
--
-- Under it, "USD/EUR at 0.92" and "USD/EUR at 0.95 WHEN country = FR" collide on
-- every column, so the conditional rule could not be stored beside the simple one at
-- all -- and a precedence rule between two rows that cannot coexist is a rule about
-- nothing. The condition therefore joins the key.
--
-- IT JOINS AS A DIGEST, NOT AS THE JSONB ITSELF. A btree entry is capped near 2704
-- bytes and a legitimate condition reaches for that bound (`country` over the 27 EU
-- members is ~190 bytes; over the ~250 the ISO vocabulary admits it is ~1750, and a
-- second key on top of it overflows). A rate a person could not save because their
-- condition was long is a defect with a stack trace instead of a sentence, so the
-- key carries a fixed 64 characters. `conditions::text` is canonical for a given
-- jsonb value -- Postgres sorts the keys on storage -- so the digest is stable, and
-- `core.fx_fixed_rates` sorts each value list before writing so that
-- ["BE","FR"] and ["FR","BE"] are ONE condition rather than two.
--
-- ---------------------------------------------------------------------------
-- THREE. THE WINDOW, PROMOTED OUT OF `derivation` SO A READ CAN HONOUR IT.
-- ---------------------------------------------------------------------------
-- `core.fx_fixed_rates` stored `valid_from`/`valid_to` inside the `derivation` JSON
-- and its own header called that "honest for storage and disclosure, and NOT yet
-- honest for resolution". Measured on the disposable base at 287, that is exactly
-- what it does: a rate posed for 2026-01-01..2026-12-31 resolves on 2027-06-01, and
-- resolves LABELLED `carry_forward` -- so the posed number both outlives its window
-- and loses the very label migration 279 exists to give it.
--
-- Nullable, because an INGESTED observation has no window and must not be made to
-- invent one: NULL here means "no declared window", which is the ingested case, and
-- it is the only reading under which the two writers can share this table honestly.
--
-- ---------------------------------------------------------------------------
-- NO ROW IS TOUCHED, AND THAT IS MEASURED.
-- ---------------------------------------------------------------------------
-- `app.fx_rate_observations` holds zero rows -- migration 279 recorded it, and
-- `SELECT count(*)` on the disposable base rebuilt at 287 confirms it (0). Every
-- column added here is therefore either NULL or takes its DEFAULT on a table with
-- nothing in it, and no backfill runs. That matters beyond tidiness:
-- `trg_fx_rate_observations_immutable` refuses EVERY update on this table, so a
-- backfill would have had to disable an immutability guard to proceed.
--
-- NO `org_id` IS ADDED. This table is scoped by `project_id` and reached only
-- through `app.fx_rate_sets`, which carries the org and its Epic-36 policy since
-- migration 273. Adding an `org_id` here would incur the RLS ratchet
-- (`test_rls_covers_every_org_scoped_table_pg.py`) for a column with no reader.

BEGIN;

-- ONE -------------------------------------------------------------------------

ALTER TABLE app.fx_rate_observations
    ADD COLUMN IF NOT EXISTS conditions JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE app.fx_rate_observations
    DROP CONSTRAINT IF EXISTS ck_fx_rate_observation_conditions;

ALTER TABLE app.fx_rate_observations
    ADD CONSTRAINT ck_fx_rate_observation_conditions
    CHECK (jsonb_typeof(conditions) = 'object');

COMMENT ON COLUMN app.fx_rate_observations.conditions IS
    'The named case this rate holds for, in the vocabulary app.fee_tax_rules has '
    'carried since migration 119: {key: [values]}, and `{}` means it holds '
    'unconditionally. FX admits connector / country / market / datastream; the '
    'closed set and the refusal for anything else live in core.fx_fixed_rates, so '
    'an unknown key is answered with a sentence rather than a constraint violation. '
    'Ratified 2026-08-17 -- the spreadsheet `IF` half of the fixed-rate amendment.';

-- THREE (before TWO: the digest is generated, so it is added last) -------------

ALTER TABLE app.fx_rate_observations
    ADD COLUMN IF NOT EXISTS valid_from DATE;

ALTER TABLE app.fx_rate_observations
    ADD COLUMN IF NOT EXISTS valid_to DATE;

ALTER TABLE app.fx_rate_observations
    DROP CONSTRAINT IF EXISTS ck_fx_rate_observation_window;

ALTER TABLE app.fx_rate_observations
    ADD CONSTRAINT ck_fx_rate_observation_window
    CHECK (
        (valid_from IS NULL AND valid_to IS NULL)
        OR (valid_from IS NOT NULL AND valid_to IS NOT NULL AND valid_to >= valid_from)
    );

COMMENT ON COLUMN app.fx_rate_observations.valid_from IS
    'First day this POSED rate holds. NULL means no declared window -- the INGESTED '
    'case, where the observation speaks for its effective_date alone. Promoted out '
    'of `derivation` by migration 288 so resolution can honour it: before that a '
    'posed rate served past the end of its own window, labelled `carry_forward`.';

COMMENT ON COLUMN app.fx_rate_observations.valid_to IS
    'Last day this POSED rate holds, inclusive. NULL only when valid_from is NULL. '
    'A posed rate is refused without both ends (core.fx_fixed_rates): a rate with no '
    'end is the `2099-12-31` the governed method replaces.';

-- TWO --------------------------------------------------------------------------

ALTER TABLE app.fx_rate_observations
    ADD COLUMN IF NOT EXISTS condition_digest TEXT
    GENERATED ALWAYS AS (encode(sha256(conditions::text::bytea), 'hex')) STORED;

COMMENT ON COLUMN app.fx_rate_observations.condition_digest IS
    'sha256 of the canonical jsonb text of `conditions`. Generated, never written. '
    'It exists so uniqueness can include the condition without putting an unbounded '
    'jsonb in a btree key -- a condition naming the ~250 ISO countries would overflow '
    'the ~2704-byte entry bound and refuse a legitimate rate with a stack trace.';

ALTER TABLE app.fx_rate_observations
    DROP CONSTRAINT IF EXISTS uq_fx_rate_observation;

ALTER TABLE app.fx_rate_observations
    ADD CONSTRAINT uq_fx_rate_observation
    UNIQUE (rate_set_version_id, base_currency, quote_currency, effective_date,
            condition_digest);

-- The read path asks "every candidate for this pair in this version, at or before
-- this date", then ranks the survivors by specificity. It reads the condition and
-- the window on every candidate, so both travel in the index rather than sending
-- the planner back to the heap once per posed rate.
DROP INDEX IF EXISTS app.idx_fx_rate_observations_pair;

CREATE INDEX IF NOT EXISTS idx_fx_rate_observations_pair
    ON app.fx_rate_observations
       (rate_set_version_id, base_currency, quote_currency, effective_date DESC)
    INCLUDE (conditions, valid_from, valid_to, condition_digest);

COMMIT;
