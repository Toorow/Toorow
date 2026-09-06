-- infra/nango/migrations/123_fee_tax_119_inplace_edit_repair.sql
--
-- Repairs a silent divergence between migration 119 and every database that had
-- already applied it.
--
-- WHAT HAPPENED
-- Story 41.5 edited 119_fee_tax_alignment.sql IN PLACE on 2026-07-27, stating in
-- the file: "This file is committed but APPLIED TO NO DATABASE, so the vocabulary
-- fix is free now and expensive forever after." That was not true of this
-- deployment: 119 had been applied at 11:40:53 the same day, hours earlier. The
-- migration ledger caught it as a checksum drift -- which is precisely what a
-- ledger is for -- but the consequence had to be measured rather than assumed.
--
-- WHY REPLAYING 119 DOES NOTHING
-- All three of 41.5's changes live INSIDE `CREATE TABLE IF NOT EXISTS
-- app.fee_tax_rules (...)`. On a database where that table already exists the
-- whole statement is a no-op, so the edit lands on a fresh database and is
-- silently inert everywhere else. Replaying 119 here changed nothing, verified by
-- reading pg_constraint before and after.
--
-- WHAT WAS ACTUALLY MISSING (measured, not inferred)
--   fee_tax_rules_form_check          lacked 'PER_TRANSACTION'
--   ck_fee_tax_rules_category_form    absent
--   ck_fee_tax_rules_form_base_target absent
-- The 41.5 evaluator writes form='PER_TRANSACTION'. Against this schema that
-- write raises a CheckViolation -- a production break waiting for the first
-- payment-fee rule.
--
-- WHY A NEW MIGRATION RATHER THAN ANOTHER IN-PLACE EDIT
-- The same fix cannot be made in 119 again: it would change 119's checksum a
-- second time and still not run, for the same reason. A constraint that must
-- change on an EXISTING table needs an ALTER, and an ALTER needs its own
-- migration number. This file adds nothing new -- every definition below is
-- copied verbatim from 119's current text, so a fresh database (which gets them
-- from the CREATE TABLE) and a repaired one end up with identical constraints.
--
-- SAFETY: app.fee_tax_rules held 0 rows when this was written, so no existing row
-- can violate the narrowed pairs. The DO block re-checks that at run time and
-- refuses rather than dropping a guard it cannot restore.
--
-- Idempotent: every constraint is dropped IF EXISTS and re-added, so re-running
-- converges on the same definitions.

BEGIN;

DO $$
DECLARE
    offending INTEGER;
BEGIN
    IF to_regclass('app.fee_tax_rules') IS NULL THEN
        RAISE NOTICE '123: app.fee_tax_rules absent -- nothing to repair';
        RETURN;
    END IF;

    -- A row written under the OLD, wider rules could fail the narrowed pair
    -- guards. Refuse loudly instead of dropping the existing check and leaving
    -- the table unguarded, or failing halfway with one constraint restored.
    SELECT count(*) INTO offending
      FROM app.fee_tax_rules
     WHERE NOT (
            (category = 'PLATFORM_FEE'   AND form IN ('PERCENTAGE', 'FLAT', 'SPEND_TIERS'))
         OR (category = 'REGULATORY_TAX' AND form IN ('PERCENTAGE', 'FLAT', 'SPEND_TIERS'))
         OR (category = 'WHT_GROSS_UP'   AND form IN ('GROSS_UP', 'PERCENTAGE', 'FLAT', 'SPEND_TIERS'))
         OR (category = 'AGENCY_FEE'     AND form IN ('PERCENTAGE', 'FLAT', 'SPEND_TIERS'))
         OR (category = 'SALES_TAX'      AND form IN ('PERCENTAGE', 'FLAT', 'SPEND_TIERS'))
         OR (category = 'VERIFICATION'   AND form = 'CPM')
         OR (category = 'PAYMENT_FEE'    AND form IN ('PERCENTAGE', 'FLAT', 'PER_TRANSACTION'))
     );
    IF offending > 0 THEN
        RAISE EXCEPTION
            '123: % existing fee_tax_rules row(s) violate the 41.5 category/form pairs; '
            'resolve them before narrowing the constraint', offending;
    END IF;

    -- 1. The form vocabulary gains PER_TRANSACTION.
    ALTER TABLE app.fee_tax_rules DROP CONSTRAINT IF EXISTS fee_tax_rules_form_check;
    ALTER TABLE app.fee_tax_rules
        ADD CONSTRAINT fee_tax_rules_form_check
        CHECK (form IN ('PERCENTAGE', 'CPM', 'FLAT',
                        'SPEND_TIERS', 'GROSS_UP', 'PER_TRANSACTION'));

    -- 2. Payload coherence: PER_TRANSACTION is FLAT-shaped -- an absolute amount
    --    charged once per transaction, so it needs the amount AND the currency it
    --    is denominated in (41.5 refuses a foreign-currency amount rather than
    --    converting it; FX is applied once, at read -- Epic 39.10).
    ALTER TABLE app.fee_tax_rules DROP CONSTRAINT IF EXISTS ck_fee_tax_rules_form_payload;
    ALTER TABLE app.fee_tax_rules
        ADD CONSTRAINT ck_fee_tax_rules_form_payload
        CHECK (
            (form IN ('PERCENTAGE', 'GROSS_UP') AND rate IS NOT NULL)
         OR (form = 'FLAT' AND amount_micros IS NOT NULL AND currency IS NOT NULL)
         OR (form = 'PER_TRANSACTION' AND amount_micros IS NOT NULL AND currency IS NOT NULL)
         OR (form = 'CPM'  AND cpm_micros IS NOT NULL AND currency IS NOT NULL)
         OR (form = 'SPEND_TIERS' AND tiers IS NOT NULL
             AND jsonb_typeof(tiers) = 'object'
             AND jsonb_typeof(tiers -> 'bands') = 'array'
             AND jsonb_array_length(tiers -> 'bands') > 0
             AND COALESCE(tiers ->> 'mode', 'cliff') IN ('cliff', 'marginal'))
        );

    -- 3. Routed-pair guard A: (category, form). A pair no evaluator routes would
    --    contribute a silent zero to the cascade.
    ALTER TABLE app.fee_tax_rules DROP CONSTRAINT IF EXISTS ck_fee_tax_rules_category_form;
    ALTER TABLE app.fee_tax_rules
        ADD CONSTRAINT ck_fee_tax_rules_category_form
        CHECK (
            (category = 'PLATFORM_FEE'   AND form IN ('PERCENTAGE', 'FLAT', 'SPEND_TIERS'))
         OR (category = 'REGULATORY_TAX' AND form IN ('PERCENTAGE', 'FLAT', 'SPEND_TIERS'))
         OR (category = 'WHT_GROSS_UP'   AND form IN ('GROSS_UP', 'PERCENTAGE', 'FLAT', 'SPEND_TIERS'))
         OR (category = 'AGENCY_FEE'     AND form IN ('PERCENTAGE', 'FLAT', 'SPEND_TIERS'))
         OR (category = 'SALES_TAX'      AND form IN ('PERCENTAGE', 'FLAT', 'SPEND_TIERS'))
         OR (category = 'VERIFICATION'   AND form = 'CPM')
         OR (category = 'PAYMENT_FEE'    AND form IN ('PERCENTAGE', 'FLAT', 'PER_TRANSACTION'))
        );

    -- 4. Routed-pair guard B: (form, base_target). Same reasoning.
    ALTER TABLE app.fee_tax_rules DROP CONSTRAINT IF EXISTS ck_fee_tax_rules_form_base_target;
    ALTER TABLE app.fee_tax_rules
        ADD CONSTRAINT ck_fee_tax_rules_form_base_target
        CHECK (
            (form = 'CPM'             AND base_target = 'MEASURED_IMPRESSIONS')
         OR (form = 'PER_TRANSACTION' AND base_target = 'GROSS_REVENUE')
         OR (form = 'GROSS_UP'        AND base_target IN ('NET_MEDIA', 'RUNNING_SUBTOTAL'))
         OR (form = 'SPEND_TIERS'     AND base_target IN ('NET_MEDIA', 'RUNNING_SUBTOTAL'))
         OR (form IN ('PERCENTAGE', 'FLAT')
             AND base_target IN ('NET_MEDIA', 'RUNNING_SUBTOTAL',
                                 'NET_REVENUE', 'GROSS_REVENUE'))
        );

    RAISE NOTICE '123: fee_tax_rules constraints realigned with 119 as committed';
END
$$;

COMMIT;
