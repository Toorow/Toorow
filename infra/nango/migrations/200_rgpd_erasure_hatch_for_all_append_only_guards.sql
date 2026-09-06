-- 200 -- 62 append-only guards still make RGPD erasure structurally impossible
--
-- 098 named the defect exactly: append-only triggers refuse the DELETE that
-- erasing an organization fires, so "dropping an org was STRUCTURALLY
-- impossible, independently of any application code" (098:8-12). Its remedy was
-- the escape hatch -- `SET LOCAL app.rgpd_erasure` set by `core/org_purge.py`,
-- honoured by the trigger. It then applied that remedy to the handful of tables
-- it had in front of it, and every append-only table added since inherited the
-- block without the hatch.
--
-- MEASURED 2026-08-03, on a database at migration 199 -- 136 row-level triggers
-- fire on DELETE and RAISE; 74 already carry the hatch and 62 do not:
--
--   SELECT count(*) FILTER (WHERE armed) AS armed,
--          count(*) FILTER (WHERE NOT armed) AS blocking
--     FROM (SELECT pg_get_triggerdef(tg.oid) ILIKE '%rgpd_erasure%'
--                  OR pg_get_functiondef(p.oid) ILIKE '%rgpd_erasure%' AS armed
--             FROM pg_trigger tg JOIN pg_proc p ON p.oid = tg.tgfoid
--             JOIN pg_class cl ON cl.oid = tg.tgrelid
--             JOIN pg_namespace n ON n.oid = cl.relnamespace
--            WHERE NOT tg.tgisinternal AND n.nspname = 'app'
--              AND (tg.tgtype & 8) <> 0 AND (tg.tgtype & 1) <> 0
--              AND tg.tgconstraint = 0 AND p.prokind = 'f'
--              AND pg_get_functiondef(p.oid) ILIKE '%RAISE%') s;
--   -> armed 74, blocking 62
--
-- THE HATCH LIVES IN TWO PLACES, AND COUNTING ONLY ONE GIVES A FALSE NUMBER.
-- A first pass of this migration searched only FUNCTION bodies and claimed 80
-- blocked guards over 98 tables. It was wrong: many guards are armed in the
-- TRIGGER's own WHEN clause instead -- `trg_connector_verification_run_protect`
-- reads `WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM
-- 'on')` and its function never mentions the flag. The mistake surfaced only
-- because re-arming an already-armed trigger raised a syntax error; had the
-- expression been shaped differently it would have passed and the number would
-- have entered the record as measured. The query above therefore reads
-- `pg_get_triggerdef` AND `pg_get_functiondef`.
--
-- 62 is still not one point that can be patched. Two had already been walked to
-- by hand this session (`org_plan_history` in 198,
-- `datastream_inbound_credential_rate_events` in 199) and
-- `business_taxonomy_versions` was next in the queue; this migration ends the
-- queue instead of advancing it by one.
--
-- WHY THE TRIGGER'S `WHEN`, AND NOT 62 REWRITTEN FUNCTION BODIES. The bodies do
-- not share a shape: `reject_business_taxonomy_version_mutation` raises
-- unconditionally in four lines, while `protect_inbound_receipt` carries its
-- DELETE branch inside a hundred lines of state-machine validation. Editing
-- those mechanically would be one chance per function to change a rule nobody
-- reviewed. The WHEN clause is also how this schema already arms most of its
-- hatches, so it introduces no idiom -- it finishes an existing one.
--
-- A `WHEN` clause changes no logic at all. It decides whether the trigger FIRES,
-- leaving every function byte-identical, and it is checked before the function
-- is entered. Outside an erasure the condition is false-y and behaviour is
-- exactly today's; inside one the guard stands aside. Verified applicable to
-- every one of the 62: all are FOR EACH ROW (statement-level triggers may not
-- carry WHEN) and none is a constraint trigger. Those that already have a WHEN
-- keep it -- the existing expression is preserved and ANDed, never replaced.
--
-- WHAT THIS OPENS, PRECISELY. The flag is set in exactly one place --
-- `org_purge.purge_org_tree`, with SET LOCAL, which dies on commit AND on
-- rollback and so cannot leak into a pooled session. Nothing else in the
-- codebase sets it (`grep -rn "rgpd_erasure" server --include="*.py"`). A
-- trigger that also guards INSERT/UPDATE stands aside for those too inside that
-- transaction, which is required rather than incidental: the purge issues
-- cycle-breaking UPDATEs that null a back-reference, and an immutability guard
-- would refuse them.
--
-- 098's rule is unchanged and is the reason this is a hatch and not a removal:
-- "UPDATE stays blocked unconditionally: append-only means history is not
-- REWRITABLE; erasure of a whole tenant is a different, audited operation."

BEGIN;

DO $$
DECLARE
    t RECORD;
    def TEXT;
    cut INT;
    when_at INT;
    head TEXT;
    tail TEXT;
    guard CONSTANT TEXT :=
        '(coalesce(current_setting(''app.rgpd_erasure'', TRUE), ''off'') <> ''on'')';
    armed INT := 0;
BEGIN
    FOR t IN
        SELECT tg.oid, tg.tgname, cl.relname, tg.tgqual IS NOT NULL AS has_when
          FROM pg_trigger tg
          JOIN pg_proc p ON p.oid = tg.tgfoid
          JOIN pg_class cl ON cl.oid = tg.tgrelid
          JOIN pg_namespace n ON n.oid = cl.relnamespace
         WHERE NOT tg.tgisinternal
           AND n.nspname = 'app'
           AND (tg.tgtype & 8) <> 0        -- fires on DELETE
           AND (tg.tgtype & 1) <> 0        -- FOR EACH ROW (WHEN needs this)
           AND tg.tgconstraint = 0         -- not a constraint trigger
           AND p.prokind = 'f'             -- pg_get_functiondef raises on aggregates
           AND pg_get_functiondef(p.oid) ILIKE '%RAISE%'
           -- Both places the hatch can live. Checking only the function body
           -- re-arms a trigger already armed in its WHEN, which raises.
           AND pg_get_functiondef(p.oid) NOT ILIKE '%rgpd_erasure%'
           AND pg_get_triggerdef(tg.oid) NOT ILIKE '%rgpd_erasure%'
    LOOP
        -- `pg_get_triggerdef` renders the whole statement, WHEN included:
        --   CREATE TRIGGER x BEFORE DELETE ON app.t FOR EACH ROW
        --     [WHEN (<expr>)] EXECUTE FUNCTION app.f()
        -- so the guard is injected just before EXECUTE FUNCTION and the rest is
        -- carried across verbatim -- timing, event list, transition tables and
        -- arguments included, none of them re-derived by hand.
        def := pg_get_triggerdef(t.oid);

        cut := position(' EXECUTE FUNCTION ' in def);
        IF cut = 0 THEN
            -- Never seen on this schema, but a silent skip here would leave a
            -- guard un-armed while the migration reported success.
            RAISE EXCEPTION 'migration 200: cannot locate EXECUTE FUNCTION in %', def;
        END IF;
        head := substr(def, 1, cut - 1);
        tail := substr(def, cut);

        IF t.has_when THEN
            -- `head` ends with `WHEN (<expr>)`. PostgreSQL wants ONE
            -- parenthesised expression, so `WHEN (<expr>) AND <guard>` is a
            -- syntax error: the clause has to be reopened and both conditions
            -- put inside it. The keyword is located by ' WHEN ' -- uppercase
            -- and space-delimited, which no identifier in this schema is.
            when_at := position(' WHEN ' in head);
            head := substr(head, 1, when_at - 1)
                    || ' WHEN (' || substr(head, when_at + 6)
                    || ' AND ' || guard || ')';
        ELSE
            head := head || ' WHEN ' || guard;
        END IF;

        EXECUTE format('DROP TRIGGER %I ON app.%I', t.tgname, t.relname);
        EXECUTE head || tail;
        armed := armed + 1;
    END LOOP;

    RAISE NOTICE 'migration 200: RGPD hatch armed on % trigger(s)', armed;
END;
$$;

COMMIT;
