-- 271 -- The second reconciliation model is retired. The tables say so (AI-295).
--
-- IT CHANGES NO DATA AND DROPS NOTHING. It writes three table comments, and that
-- is the whole point.
--
-- WHAT HAPPENED. `app.overlap_groups`, `app.overlap_group_members` and
-- `app.reconciliation_rules` were the SECOND model answering « do these two
-- connectors count the same thing » -- the first being the governed Rule Sets.
-- Story 49.4 stopped the runtime reading them; `ee6255b9` stopped the two read
-- surfaces; and `import_platform_defaults` no longer writes them, so the
-- bootstrap route that filled them on every org creation fills nothing.
--
-- Measured 2026-08-17, on a database with every migration applied:
--
--   app.overlap_groups           0 rows
--   app.overlap_group_members    0 rows
--   app.reconciliation_rules     0 rows
--
--   live SQL touching any of the three, across server/ excluding tests: NONE.
--   The fourteen remaining mentions are all docstring prose explaining the
--   retirement.
--
-- WHY A COMMENT AND NOT A DROP. `import_platform_defaults` names the trap in its
-- own docstring: « a store that nothing reads but a bootstrap route still fills
-- is a trap -- the next reader finds rows and believes them ». The route no
-- longer fills them, so the rows are gone; what remains is the SHAPE, and a
-- reader who greps the schema still finds three tables whose names promise a
-- reconciliation model. A comment is what turns that promise into a date.
--
-- A DROP is irreversible, and a permanent authorisation never authorises the
-- irreversible. It would also be premature: `metric_reconciliation.py` still
-- explains its own behaviour by contrast with these tables, so a reader
-- following that prose must be able to look them up. The drop belongs to
-- whoever owns epic 49's close, with the prose retired in the same commit.
--
-- WHAT IS NOT CLAIMED: that reconciliation is finished. Only that there is ONE
-- model of it -- the governed Rule Sets -- and that these three tables are not
-- part of it.
--
-- Schema-Change-Checklist:
--   [x] Additive and replayable (COMMENT ON is idempotent)
--   [x] No column altered, no row read or written
--   [x] No table dropped
--
-- ERASURE: creates no table. These three carry org-scoped rows in principle and
-- are reached by `core.org_purge` through their existing foreign keys; nothing
-- here changes that, and they hold no rows to erase.

BEGIN;

COMMENT ON TABLE app.overlap_groups IS
    'RETIRED 2026-08-17 (AI-295). The second reconciliation model. Nothing reads '
    'this table and nothing writes it: the runtime stopped in Story 49.4, the two '
    'read surfaces in ee6255b9, and import_platform_defaults no longer fills it on '
    'org bootstrap. The ONE reconciliation model is the governed Rule Sets. Rows '
    'found here would be pre-governance residue, not a source of truth.';

COMMENT ON TABLE app.overlap_group_members IS
    'RETIRED 2026-08-17 (AI-295). Members of the retired second reconciliation '
    'model -- see app.overlap_groups. It addressed sources by CONNECTOR name, '
    'which is exactly what the governed Rule Sets replaced.';

COMMENT ON TABLE app.reconciliation_rules IS
    'RETIRED 2026-08-17 (AI-295). Rules of the retired second reconciliation '
    'model -- see app.overlap_groups. A rule here is not applied by any code '
    'path; the governed Rule Sets carry the live ones.';

COMMIT;
