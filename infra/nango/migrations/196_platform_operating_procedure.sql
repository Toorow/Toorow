-- 196: the platform operating procedure -- how to connect, map, activate, drive
-- and TEST a Datastream, stored as the object this platform already governs for
-- "how to do X": a Procedure (`app.procedures`), at PLATFORM scope.
--
-- WHY THIS EXISTS. Jean, literally: "techniquement parlant tu devrais avoir dans
-- la plateforme de base un skill dispo qui explique ta demarche de tests, la
-- structure, comme un peu un quick access pour comment connecter, mettre a jour,
-- utiliser les datastream et le context." There is no `skills` table and there
-- does not need to be one: the governed "how to do X" object already exists, it
-- is versioned (`app.procedures_versions`, append-only since 031), it is citable
-- BY NAME (`core/context_search.get_procedure_by_name`), it is searchable
-- (`core/context_search.search_context`), and reading it is auditable -- the AI
-- Path classifies `get_procedure` at the PROCEDURE rung
-- (`core/ai_path_recorder._PROCEDURE_TOOL_NAMES`). A file in the repository has
-- none of those properties from inside a session.
--
-- THE OBJECTION THIS MIGRATION MUST ANSWER, and why it does not apply.
-- This repository FORBIDS seeding demonstration content in a migration. It
-- happened in 095/096 -- three knowledge entries and five metric procedures
-- signed "Winston (Architect)", "Mary (Analyst)", "Paige (Tech Writer)"
-- (agent personas presented as human authors), plus eight golden questions and
-- six evaluation runs with fabricated scores -- and 120 purged all of it. The
-- rule that came out of it: an empty screen tells the truth ("nothing is defined
-- yet"), a populated fake one lies, and a fabricated score reads as a
-- measurement.
--
-- This row is a different object, on three counts that are checkable, not
-- rhetorical:
--   1. It is not the DATA OF A SCREEN. It populates no product surface with
--      invented tenant content; it adds no row to any project. It is
--      `project_id IS NULL` -- deployment documentation, like `app.platform_clocks`
--      (195) is deployment infrastructure.
--   2. It fabricates NOTHING. Every statement in the body is either a row count
--      taken on 2026-08-02 with the query printed next to it, or a citation of a
--      file and symbol in this repository. Where a step has never been executed,
--      the body SAYS SO instead of describing it -- see its sections 8 and 9.
--      There is no persona: the author is `migration-196`.
--   3. Its ABSENCE is the defect. Six sessions in a row re-measured the same
--      three closed doors and the same two Windows measurement traps, because
--      what is not in the repository is lost when a conversation ends
--      (CLAUDE.md, section 7). Storing the measurement once, dated, with its
--      command, is the opposite of inventing one.
-- If the body ever stops matching what a re-run of its own commands returns, the
-- fix is a new version through `context_store.update_procedure` (or a later
-- migration), never an edit of this file: an applied migration is immutable
-- (`scripts/apply_migrations.py` checksum drift + `manifest.json`).
--
-- SCOPE. `project_id IS NULL` is supported and load-bearing, not a shortcut:
-- `core/context_search.py` reads procedures with
-- `WHERE status='active' AND (project_id IS NULL OR project_id = %s)`, and
-- `get_procedure_by_name` orders `(project_id IS NULL) ASC`. So this text is
-- readable from every project, AND a project that needs its own variant defines
-- a procedure with the same name and silently wins. Nothing here forces itself
-- on a tenant.
--
-- IDEMPOTENCE. The id is a fixed constant, not a generated ULID, so a replay is
-- a no-op by construction. Two guards, because there are two ways a duplicate
-- could appear: the id, and the partial unique index on the platform-scope NAME
-- (`uq_procedures_name_platform`, 031). If either is already taken the migration
-- RAISEs a NOTICE and leaves the existing row untouched -- including a row a
-- human has since edited. Never overwrite what you did not author.
--
-- WHAT THIS MIGRATION DELIBERATELY DOES NOT DO.
--   * It writes NO audit row. `context_store.create_procedure` writes one
--     because it acts for an identity; a migration is not an identity, and its
--     trail is the migration ledger. It also keeps this file runnable against a
--     schema where `app.audit_log` is not present.
--   * It does not write the `owner` column (added by 118) on either table, so
--     the file applies to a database built from 031 alone -- which is what the
--     test harness does.
--   * It creates no table, drops nothing, alters nothing. Two INSERTs.

BEGIN;

DO $migration$
DECLARE
    -- Fixed, deliberately non-random, ULID-shaped (26 Crockford base32
    -- characters). A generated id would make a replay insert a second row.
    v_proc_id CONSTANT TEXT := 'proc_0000000DATASTREAMTESTSPATH';
    v_actor   CONSTANT TEXT := 'migration-196';
    -- The NAME is the citation key: `get_procedure(name=...)` resolves by name,
    -- not by id. Keep it short, lowercase and stable.
    v_name    CONSTANT TEXT := 'platform-operating-procedure';
    -- Explicit `||` rather than adjacent string literals: the concatenation is
    -- then unambiguous inside a plpgsql DECLARE default expression.
    v_desc    CONSTANT TEXT :=
        'Quick access for an agent: connect a source, map it, activate it, drive '
        || 'its cadence, land data, read the output -- and how to measure each step '
        || 'without lying to yourself. Dated 2026-08-02; every number carries its '
        || 'command. Names what is still closed instead of describing it.';
    v_now              TIMESTAMPTZ := now();
    v_frontmatter      TEXT;
    v_body             TEXT;
BEGIN
    IF to_regclass('app.procedures') IS NULL THEN
        RAISE NOTICE '196: skip -- app.procedures does not exist';
        RETURN;
    END IF;

    IF EXISTS (SELECT 1 FROM app.procedures WHERE id = v_proc_id) THEN
        RAISE NOTICE '196: procedure % already present -- nothing to do', v_proc_id;
        RETURN;
    END IF;

    -- The platform-scope name is unique among non-archived rows (031). If a row
    -- already holds it, it is either a re-run under another id or somebody's own
    -- procedure: in both cases, leave it alone.
    IF EXISTS (
        SELECT 1 FROM app.procedures
        WHERE project_id IS NULL AND name = v_name AND status <> 'archived'
    ) THEN
        RAISE NOTICE
            '196: a platform procedure already holds the name % -- left untouched',
            v_name;
        RETURN;
    END IF;

    v_frontmatter := $procfm$name: platform-operating-procedure
description: >-
  Quick access for an agent: connect a source, map it, activate it, drive its
  cadence, land data, read the output -- and how to measure each step without
  lying to yourself. Dated 2026-08-02; every number carries its command.
mdm_tags:
  - platform-operating-procedure
  - datastream
  - context
  - testing
  - measurement
tool_bindings:
  - step: 1
    tool: get_datastream_readiness
  - step: 2
    tool: inspect_mapping
  - step: 3
    tool: propose_mapping_correction
  - step: 4
    tool: test_mapping_candidate
  - step: 5
    tool: review_agent_change
  - step: 6
    tool: confirm_agent_change
  - step: 7
    tool: get_datastream_schedule
  - step: 8
    tool: set_datastream_schedule
  - step: 9
    tool: prepare_datastream_recovery
  - step: 10
    tool: confirm_datastream_recovery
  - step: 11
    tool: list_datastream_runs
  - step: 12
    tool: analyze_result
  - step: 13
    tool: list_platform_clocks
  - step: 14
    tool: set_platform_clock_cadence
  - step: 15
    tool: apply_platform_clock
  - step: 16
    tool: run_platform_clock_now
  - step: 17
    tool: metric_definition_upsert
  - step: 18
    tool: metric_mapping_confirm
  - step: 19
    tool: search_context
  - step: 20
    tool: get_procedure
$procfm$;

    v_body := $procbody$# Platform operating procedure

Quick access: how to connect a source, map it, activate it, drive its cadence,
land data, read the output -- and how to test any of that without lying to
yourself.

Cite it with `get_procedure(name="platform-operating-procedure")`. A project that
needs a different answer defines a procedure with the same name: the
project-scoped row wins over this one
(`core/context_search.get_procedure_by_name`, `ORDER BY (project_id IS NULL) ASC`).

**Every number below is dated 2026-08-02 and carries the command that produces
it. Re-run the command before you rely on the number.** A fact with no date and
no command is not a fact.

---

## 1. What this is, and what it is not

It IS the platform's own operating documentation, derived from measurement.

It is NOT:

* **not the ratified target.** That is `docs/product-architecture/` -- one
  document per surface, each ending with an "Incomplete if" list. Open the
  surface's document before changing the surface. This procedure tells you how
  the machine behaves, never what it should be.
* **not a claim that the chain works.** Section 8 names what is closed, and why.
* **not project content.** This row is `project_id IS NULL`: platform scope,
  readable from every project, owned by no tenant.

Reading it is auditable. `get_procedure` is classified at the `PROCEDURE` rung of
the AI Path (`core/ai_path_recorder._PROCEDURE_TOOL_NAMES`), so an agent
consulting this text leaves the same kind of trace as any other crossing. That is
deliberate: a procedure nobody can prove was read is a procedure nobody read.

---

## 2. The chain, as measured on 2026-08-02

42 Datastreams exist. **Exactly one** carries a mapping: Google Search Console,
module `gsc` -- credential active, `connection_account_scope = 'ready'`,
`enabled = true`, plan present, mapping present.

    app.datastream_output_versions                    0 rows
    app.datastreams WHERE lifecycle_state = 'active'  0 rows
    app.datastream_executions                         0 rows
    app.datastream_setup_drafts                       empty

**Nothing on this platform has ever produced an output.** Treat every screen
downstream of a pull as unproven until you have produced the first execution
yourself. "The tests pass" is not evidence that a path exists; see section 4.

---

## 3. The sequence of a Datastream -- what exists, what is missing

### Step 1 -- Create

**Exists.** 42 rows in `app.datastreams` prove the creation path end to end.

### Step 2 -- Credential

**Exists.** One Datastream holds an active credential with
`connection_account_scope = 'ready'`. Google connectors (GA4, Search Console,
Google Ads, Sheets, ...) share ONE direct OAuth consent, which matters in step 7:
a single credential opens seven connectors, so `provider = 'google'` does not
identify a module.

### Step 3 -- Plan

**Exists.** `app.datastream_plan_versions`; the pointer is
`app.datastreams.current_plan_version_id`. Plan versions are append-only.

### Step 4 -- Mapping

**Exists, and it is the step an agent can drive today.**

* Governed writer: `datastream_change.confirm_change`, which runs inside
  `operations.execute_operation` -- the seam that produces the audit row and the
  outbox entry. A writer that skips it lands a correct row and no trail; that is
  a real incident, guarded by
  `server/tests/core/test_mapping_writes_are_governed.py`.
* MCP path, verified tool by tool: `inspect_mapping` ->
  `propose_mapping_correction` -> `test_mapping_candidate` ->
  `review_agent_change` -> `confirm_agent_change`
  (`core/mapping_proposal_mcp.py`, `core/governance_mcp.py`). A confirmed change
  can be undone with `rollback_agent_change`.
* Metric definitions are editable from the same place: `metric_definition_upsert`,
  and `metric_mapping_confirm` / `metric_mapping_rename` / `metric_mapping_reject`
  (`core/metric_semantics_mcp.py`).

### Step 5 -- Activation

**Missing in practice, and the reason is circular.**

* The ONLY writer of `app.datastreams.lifecycle_state` is
  `datastream_activation.publish_activate_mutation`, reachable only from the
  materialization of a setup draft.
* `app.datastream_setup_drafts` is empty.
* Governed activation requires `current_published_execution_id` -- a candidate
  execution -- and `app.datastream_executions` has 0 rows.

So: activation needs an execution, and (section 8) an execution needs a pull that
the three available doors refuse. Nothing here is broken; the last link was never
built.

### Step 6 -- Cadence

**Exists, readable and writable.** `app.datastream_schedule_state`, MCP
`get_datastream_schedule` / `set_datastream_schedule` (`core/schedule_mcp.py`),
Processing tab of the Datastream. But cadence alone fires nothing: the nightly
loop also requires `lifecycle_state = 'active'` (step 5). See section 4 for the
full predicate and the query that tells you which condition is false.

### Step 7 -- Pull

**Closed. Three doors, three different reasons.** See section 8.

### Step 8 -- Landing

**Never executed here.** No execution exists, so no landing has ever been
observed on this deployment. Anything written about this step would be a
description of code, not a measurement -- so this procedure does not write it.

### Step 9 -- Output and analysis

**0 rows in `app.datastream_output_versions`.** `analyze_result` and
`app_read_result_manifest` (`core/analyze_render_mcp.py`) exist and read a
Result's provenance and freshness, but no Result has ever existed here for them
to read.

---

## 4. Measure before you assert

### The chain, in one query

Run against the platform database. `TEST_POSTGRES_DSN` must NEVER point at it.

```sql
SELECT
  (SELECT count(*) FROM app.datastreams)                                 AS datastreams,
  (SELECT count(*) FROM app.datastreams WHERE enabled)                   AS enabled,
  (SELECT count(*) FROM app.datastreams
    WHERE lifecycle_state = 'active')                                    AS active,
  (SELECT count(*) FROM app.datastreams
    WHERE current_mapping_version_id IS NOT NULL)                        AS mapped,
  (SELECT count(*) FROM app.datastream_executions)                       AS executions,
  (SELECT count(*) FROM app.datastream_output_versions)                  AS outputs,
  (SELECT count(*) FROM app.datastream_setup_drafts)                     AS setup_drafts;
```

### Why the nightly loop skips a given Datastream

`core/scheduler.py` selects a Datastream for the nightly dispatch only when ALL
of these hold. The query below prints them one column per condition, so the
answer is the `false` you can see rather than the cause you guessed.

```sql
SELECT ds.id,
       ds.name,
       ds.enabled                                             AS c1_enabled,
       ds.lifecycle_state = 'active'                          AS c2_active,
       ds.current_plan_version_id    IS NOT NULL              AS c3_plan,
       ds.current_mapping_version_id IS NOT NULL              AS c4_mapping,
       ds.schedule_mode = 'nightly'                           AS c5_nightly,
       ss.datastream_id IS NOT NULL                           AS c6_schedule_state,
       (ss.next_run_at IS NULL OR ss.next_run_at <= now())    AS c7_due,
       COALESCE(ds.source_kind, 'connector_pull') <> 'external_bq'
                                                              AS c8_not_external_bq,
       ss.next_run_at
FROM app.datastreams ds
LEFT JOIN app.datastream_schedule_state ss
       ON ss.datastream_id   = ds.id
      AND ss.project_id      = ds.project_id
      AND ss.plan_version_id = ds.current_plan_version_id
ORDER BY ds.id;
```

Two conditions are easy to misread:

* `next_run_at IS NULL` does NOT mean "never scheduled" -- it means "due now".
  NULL keeps the pre-existing behaviour on purpose.
* the connector activation join is a LEFT JOIN with
  `pm.enabled IS NULL OR pm.enabled = TRUE`: a connector that was never activated
  for the project does NOT block the dispatch. Do not go looking there first.

And a counter that lies: `missed_run_count = 0` is not health. A clock that never
fires leaves nothing behind, so the counter that would record the miss is never
written either.

### Tests

```bash
# from the repository root -- see the second trap in section 5
python -m pytest server/tests/core -q
ruff check server
python -m pytest --collect-only -q server/tests/core   # fast collection gate
```

---

## 5. Two measurement traps, each of which costs a day if you find it yourself

### 1. A short `--basetemp` is mandatory on Windows

The address of a quarantine object nests two SHA-256 hashes: **269 characters
against a `MAX_PATH` of 260**, with `LongPathsEnabled = 0` on this machine. Nine
inbound tests go red, and the failure presents as `FileNotFoundError`
**immediately after a `mkdir` that succeeded** -- exactly the shape that sends
you diagnosing the wrong suspect.

Proof, changing nothing but the temporary root: `--basetemp=C:/qt/b` ->
**32 passed** (was 9 failed), zero product lines touched.

```bash
python -m pytest server/tests/core -q --basetemp=C:/qt/b
```

### 2. Anchor paths on the file, never on the working directory

A test that resolves its artifacts relative to the current directory gives TWO
verdicts for the SAME tree depending on where pytest was launched (measured: 4
red from `server/`, 13 passed from the root). A non-regression gate that depends
on the launch directory guards nothing.

The model to copy is `server/tests/core/test_mapping_writes_are_governed.py:32`:

```python
ROOT = Path(__file__).resolve().parents[3]
```

---

## 6. Platform clock vs Datastream cadence -- never confuse them

They are two different objects, and mixing them up produces changes that appear
to work and change nothing.

**The platform heartbeat.** Seven Cloud Scheduler jobs, one set per deployment,
invisible to any end user, running whether a Datastream exists or not.

* Registry: `app.platform_clocks` (migration 195). Screen: `/platform/clocks`.
* MCP: `list_platform_clocks`, `get_platform_clock`, `set_platform_clock_cadence`,
  `apply_platform_clock`, `run_platform_clock_now` (`core/platform_clocks_mcp.py`).
* The registry keeps `declared_*` and `observed_*` as two distinct column sets.
  `reconcile` OBSERVES and never corrects: silently re-imposing the declared
  value would destroy the only evidence that somebody edited a clock by hand.
  A correction is `apply`, on ONE explicitly named clock.

**A Datastream's cadence.** Per Datastream, a product setting, owned by the
project.

* Table: `app.datastream_schedule_state`. Screen: Processing tab.
* MCP: `get_datastream_schedule` / `set_datastream_schedule`.

Changing a platform clock does not change a Datastream's cadence, and vice versa.

---

## 7. What an agent can reach from MCP today -- and what it cannot

Verified tool by tool.

**Can:**

* read and edit metric definitions -- `metric_definition_upsert`, `metric_reference`,
  `metric_route`;
* confirm / rename / reject a mapping -- `metric_mapping_confirm`,
  `metric_mapping_rename`, `metric_mapping_reject`;
* inspect, propose and dry-run a mapping correction -- `inspect_mapping`,
  `propose_mapping_correction`, `test_mapping_candidate`;
* review, confirm and roll back a governed change -- `review_agent_change`,
  `confirm_agent_change`, `rollback_agent_change`;
* read a Datastream's readiness and its runs -- `get_datastream_readiness`,
  `list_datastream_runs`;
* read and write a cadence -- `get_datastream_schedule`, `set_datastream_schedule`;
* prepare and confirm a recovery, which is what CREATES a candidate execution --
  `prepare_datastream_recovery`, `confirm_datastream_recovery`,
  `propose_datastream_recovery`;
* analyse a Result and read its provenance and freshness -- `analyze_result`,
  `app_read_result_manifest`, `app_read_result_slice`;
* read and change project capabilities under confirmation --
  `read_project_capability`, `preview_project_capability_impact`,
  `prepare_project_capability_change`, `confirm_project_capability_change`;
* read the context layer -- `search_context`, `get_procedure`.

**Cannot:**

* **enqueue a pull.** No module named `*_mcp.py` calls `core.queue.enqueue_pull`.
  The only callers are `core/admin_api.py`, `core/account_topology.py` and the
  scheduler.
* **write `lifecycle_state`.** The single writer is
  `datastream_activation.publish_activate_mutation`, reachable only from the
  materialization of a setup draft (step 5).

Those two absences are the whole distance between "an agent can adjust the
platform" and "an agent can run it".

---

## 8. Still closed, as of 2026-08-02 -- the three doors to a pull

Each door is shut for its own reason. Fixing one does not open the others.

### `POST /api/datastreams/{id}/run` -> 422 `dispatch_not_available`

The handler refuses as soon as the Datastream is `versioned`, and `versioned` is
simply `current_plan_version_id is not None` (`core/datastreams.py`, `_row_to_ds`).
**It therefore refuses the Datastream PRECISELY BECAUSE IT IS ARMED.** The
message points at Story 12.6, which was never built.

### `POST /api/connections/{id}/pull` -> 409

It calls `enqueue_pull` WITHOUT a `datastream_id`, so module resolution falls back
to `provider`, which is `'google'` -- the name of no module, because one direct
Google consent opens seven connectors. The wall names itself:
`multi_tool_credential_needs_a_datastream` (`core/account_topology.py`).

### Governed activation

It requires `current_published_execution_id`, hence a prior candidate execution.
`app.datastream_executions` has 0 rows and `app.datastream_setup_drafts` is
empty, so there is nothing to publish.

**Consequence for planning:** the shortest measured path to a first execution
runs through `prepare_datastream_recovery` / `confirm_datastream_recovery`, which
is the one MCP pair that creates a candidate execution. It has not been executed
end to end on this deployment either -- if you run it, record what happened here.

---

## 9. What this procedure does NOT say, and why

Written down rather than smoothed over. A procedure that reads complete while
half of it was never executed is worse than a short one.

* **Landing and output have never been observed here.** Steps 8 and 9 describe no
  keystrokes because there is no measurement to describe. Do not infer them from
  the code.
* **Adding a column end to end is only half measured.** The governed mapping
  write (`confirm_change` inside `execute_operation`) and its MCP path are
  verified; what a new column becomes downstream -- landing, marts, a Result --
  is not, for the same reason as above.
* **Reconciliation and merge across sources** are not covered: no Result has ever
  been produced on this platform, so nothing about merge behaviour here is a
  measurement.
* **Connector-specific setup** is out of scope. This is the shape of the chain,
  not a per-connector guide.

When you close one of these gaps, edit the procedure through
`context_store.update_procedure` -- it appends a version rather than overwriting
one -- and date what you measured. What is not written here is lost when the
session ends.
$procbody$;

    INSERT INTO app.procedures (
        id, project_id, name, description, frontmatter_yaml,
        body_md, status, created_by, created_at, updated_at
    )
    VALUES (
        v_proc_id, NULL, v_name, v_desc, v_frontmatter,
        v_body, 'active', v_actor, v_now, v_now
    );

    -- Version 1, exactly as `context_store.create_procedure` would append it:
    -- a procedure with no version row reads as authorless, and every reader that
    -- resolves authorship fails closed on that.
    INSERT INTO app.procedures_versions (
        procedure_id, project_id, name, description, frontmatter_yaml, body_md,
        status, created_by, created_at, updated_at, version_number,
        changed_by, changed_at
    )
    VALUES (
        v_proc_id, NULL, v_name, v_desc, v_frontmatter, v_body,
        'active', v_actor, v_now, v_now, 1,
        v_actor, v_now
    );

    RAISE NOTICE '196: platform operating procedure % inserted (name=%)',
        v_proc_id, v_name;
END
$migration$;

COMMIT;
