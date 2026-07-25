# Contributing to toorow

This is the greenfield monorepo for the modular MCP marketing-reporting platform
("open Atlas"). Story 1.1 delivers a runnable skeleton that already enforces the
architecture's ground rules.

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | **3.12** | pinned in `pyproject.toml` `requires-python` |
| uv | latest | Python env + lockfile manager (do NOT use pip/poetry) |
| Node.js | **≥ 22** | for the Vite 8 build |
| pnpm | **9.x** | JS workspace manager (do NOT use npm/yarn) |
| Terraform | ≥ 1.7 | infra validate only (apply is human-gated) |
| GNU make | optional | convenience targets; native PowerShell users run the raw commands below |

## Monorepo layout

```
connector/
  pyproject.toml   uv workspace root (members: server, dbt)
  uv.lock          Python lockfile (source of truth)
  Makefile         dev targets
  server/          FastMCP core (+ modules, populated later)
  ui/              pnpm workspace: tokens, shell, widgets/*, admin
  dbt/             dbt-core scaffold
  infra/           Terraform IaC (human-gated apply)
  .github/workflows/ci.yml
```

## Run the MCP server

```bash
make dev
# or, without make:
uv sync
PORT=8000 uv run --package toorow-server python -m core.main
```

The server runs **FastMCP 3.4.x over streamable HTTP**, binding `0.0.0.0:$PORT`
(default 8000). Endpoint: `http://localhost:8000/mcp`. See
`server/core/README.md` for transport + 421/Host-header details.

### Verify the health tool

Streamable HTTP requires an MCP handshake (JSON-RPC `initialize`, then
`tools/call`), so a plain `curl` GET will not exercise the tool. Use the FastMCP
in-process client (no network handshake needed):

```bash
uv run python - <<'PY'
import asyncio
from fastmcp import Client
from core.main import mcp

async def main():
    async with Client(mcp) as c:
        res = await c.call_tool("health", {"project_id": "default"})
        print(res.structured_content or res.data)

asyncio.run(main())
PY
# Expect the AD-1 envelope with data.status == "ok".
```

Against a running HTTP server, point the client at the URL instead:
`Client("http://localhost:8000/mcp")`.

## Build the sample widget (single-file)

```bash
make build-widget          # -> ui/widgets/sample/dist/index.html (one self-contained file)
make bundle-check          # AD-11 gate: fails on any external http(s) reference
# or raw:
pnpm -C ui install
pnpm -C ui --filter @toorow/widget-sample build
node ui/scripts/bundle-check.mjs ui/widgets/sample/dist/index.html
```

## Seed loop quick start (Story 1.4 — local data pipeline)

The GA4 seed pipeline generates 90 days of realistic data, loads it into a local
DuckDB file, runs dbt models, and verifies data quality — all without any live GCP.

**Prerequisites:** Python 3.12, `uv` (deps installed by `uv sync --all-packages`).

### One-shot runner

```bash
# From repo root — generates CSV, loads DuckDB, runs dbt + tests:
uv run python server/modules/google-analytics/seeds/run_local_loop.py
```

### Step-by-step

```bash
# 1. Generate 90-day seed CSV (1350 rows: 90d × 3 devices × 5 countries)
uv run python server/modules/google-analytics/seeds/generate_seed.py

# 2. Load into local DuckDB — mints pull_id, appends rows (append-only, AD-7)
uv run python server/modules/google-analytics/seeds/load_seed.py \
    --mode duckdb \
    --duckdb-path server/modules/google-analytics/seeds/local.duckdb

# 3. Run dbt staging + mart models
cd dbt
dbt run --select google_analytics --profiles-dir profiles

# 4. Run dbt data quality tests
dbt test --select google_analytics --profiles-dir profiles
```

> **Note:** `dbt/profiles/profiles.yml` is gitignored. The one-shot runner
> (`run_local_loop.py`) creates it automatically from `profiles.yml.example`.
> If running dbt manually, create it first or use `run_local_loop.py`.

### Verify the GA4 tool reads from the mart

```bash
uv run python - <<'PY'
import asyncio, os
os.environ["TOOROW_DB_MODE"] = "duckdb"
os.environ["TOOROW_DUCKDB_PATH"] = "server/modules/google-analytics/seeds/local.duckdb"
from fastmcp.client import Client, FastMCPTransport
from core.main import mcp

async def main():
    async with Client(FastMCPTransport(mcp)) as c:
        res = await c.call_tool("google-analytics_get_ga4_report", {})
        import json; print(json.dumps(res.structured_content or res.data, indent=2, default=str))

asyncio.run(main())
PY
# Expected: AD-1 envelope with meta.provenance = "pull_<ULID>" and non-empty data.metrics
```

### Switching to real BigQuery

1. Create `~/.dbt/profiles.yml` from `dbt/profiles/profiles.yml.example`, choosing the `bigquery` target.
2. Run the loader in BigQuery mode: `uv run python server/modules/google-analytics/seeds/load_seed.py --mode bigquery --bq-project <gcp-project>`
3. Run dbt: `cd dbt && dbt run --select google_analytics --target bigquery`

No SQL model changes required — only the dbt profile target changes.

### Environment variables for the MCP server

| Variable | Default | Description |
|---|---|---|
| `TOOROW_DB_MODE` | `duckdb` | `duckdb` \| `bigquery` |
| `TOOROW_DUCKDB_PATH` | `seeds/local.duckdb` | Path to local DuckDB warehouse |
| `GCP_PROJECT` | _(none)_ | GCP project ID (bigquery mode only) |

## CI gates (`.github/workflows/ci.yml`)

1. **Python** — `ruff check server` + `pytest server/tests` (+ `python --version`
   asserts 3.12).
2. **Widget build** — `pnpm --filter @toorow/widget-sample build`.
3. **Bundle gate (NFR4 / AD-11)** — scans `dist/index.html` for `http(s)://` and
   **fails the build** if any external reference is found.
4. **Terraform** — `terraform validate` only (never applies; apply is
   human-gated, see `infra/README.md`).

The Python/conformance jobs also run the required all-built-in capability gate:

```bash
uv run pytest server/tests/conformance/test_all_module_capabilities.py -q
```

## Local smoke test (T8.2)

On a clean clone:

```bash
uv sync
make smoke        # builds the widget, runs the bundle gate, imports the server
make dev          # start the server; verify the health tool as shown above
```

## Conventions

- **No secrets in code.** Config via env vars; secrets only in GCP Secret
  Manager or Nango. The real `dbt/profiles/profiles.yml` and Terraform
  `*.tfvars`/`backend.tf` are gitignored.
- **Module folders** are kebab-case (`google-analytics`); files inside are fixed
  names (`connector.py`, `manifest.json`, `reports/`) — AD-2.
- **Widgets** compile to one self-contained HTML file, fonts inlined — AD-11.

## Development Conventions

These conventions are derived from Epic 1 retrospective action items (AI-01,
AI-02, AI-03) and apply to all contributors including automated dev agents.

### AI-01 — Shell-denial verification pattern

Dev agents that cannot execute shell commands must annotate any BLOCKED
verification step with the exact command that needs to run rather than marking
the step complete. The orchestrator is responsible for running verification
commands and fixing any green-only regressions.

Example annotation:

```
# BLOCKED: Docker daemon not available in this environment.
# Verification command (run manually once Docker is available):
#   docker build -f infra/docker/mcp-server/Dockerfile -t connector/mcp-server:local .
#   docker run --rm -e PORT=8080 -p 8080:8080 connector/mcp-server:local
```

### AI-02 — No private framework API access

Never access attributes starting with `_` on FastMCP (or any framework) objects.
Always read the installed source in `.venv/lib/python3.12/site-packages/` to
confirm the public API surface before using an API not in the official
documentation.

Rationale: private attributes are implementation details that can change between
patch releases without notice. Story 1.1 review (finding M2) identified this as
a source of silent regressions.

### AI-03 — ASCII-only stdout

All Python scripts printing to stdout must use ASCII-only characters
(no `->`, `v/`, `x/`, `...`). If Unicode output is required, declare
`sys.stdout = open(sys.stdout.fileno(), 'w', encoding='utf-8', closefd=False)`
at the top of the script.

Rationale: non-ASCII characters in stdout cause `UnicodeEncodeError` in
environments with a non-UTF-8 default locale (e.g. some CI runners, Windows
terminals). The CI `python` job will fail silently or with an unhelpful error
if this convention is not followed.

### AI-13 — Live-integration pass for new external APIs

Any story (or change) that introduces a NEW external API — or the first real
use of one — REQUIRES a live integration pass against the real service (or its
official docker image) before it is marked done. Mocked tests encode
assumptions; the live contract must be verified. Record the live-pass evidence
in the story's Dev Agent Record. (Lesson: Story 2.2 Nango contract violations —
image tag, port mapping, and required query params all differed from the mocks.)
The story template (`.claude/skills/bmad-create-story/template.md`) carries this
as a Definition of Done checkbox.

### AI-32 / AI-41 — Observability signals

**`ALERT_TIMEOUT_SECONDS`** (default `60`): per-step soft timeout for nightly
scheduler steps. If a step's wall-clock duration exceeds this value, a `WARNING`
is emitted and the step is recorded as degraded. Synchronous / Windows-safe; no
threads are killed.

**`scheduler_step_degraded`** (`app.alert_firings`, `type` column): written once
per nightly run when at least one step failed (raised) or exceeded
`ALERT_TIMEOUT_SECONDS`. The `message` field lists all affected step names.

**`nango_revoke_failed`** (`app.alert_firings`, `type` column): written
per-connection when Nango token revocation raises during
`DELETE /api/projects/{id}`. Carries `project_id`, `connection_ref_id`,
`nango_connection_id`, and `error` in the metadata suffix of the `message`
field. Archival always completes regardless (best-effort semantics).

### AI-44 — Nango migration file naming

Migrations in `infra/nango/migrations/` MUST be named `NNN_description.sql` with
a zero-padded 3-digit numeric prefix (`001_...` … `021_...`). There is no local
apply script; migrations are applied in numeric order (CI orders them with
`sort -t_ -k1,1 -k2,2n` — commit 433c1ef — which is only correct because the
prefix is zero-padded). Never reuse or renumber an applied prefix; the next
migration always takes the next free number.

## dbt Mart Template Rules

These rules apply to every mart model added in Epic 3 and beyond. They are
derived from retrospective action item AI-05 (Epic 1) and are mandatory for
all code-review sign-offs.

### AI-05 — Grain uniqueness test (mandatory)

Every mart model MUST declare a `unique` test on its full grain key in
`schema.yml`. Pattern: see `dbt/models/marts/schema.yml` ->
`fact_daily_kpi_grain_unique`.

```yaml
tests:
  - unique:
      name: <model_name>_grain_unique
      arguments:
        column_name: "col_a || '|' || col_b || '|' || col_c"
```

The grain key must name ALL columns that together identify one logical row.

Rationale: a missing grain test means double-counted KPIs reach the widget.
This was caught in review-1-4 finding F-05 and tracked as AI-05. New mart
models that skip this test will fail code review.

### AI (Story 39.7) — Report-timezone capture (mandatory for daily connectors)

Every daily connector lands `report_timezone` as per-row provenance, read via the
`time_context` contract (declared on its `source_capabilities` descriptor); undetermined =>
a recorded assumption (`assumed=true`) or a `TIMEZONE_GAP`, never a silent zone. It is
CAPTURE-only provenance — passed through staging UNCHANGED, never `convert_timezone()`d at
day grain (HG-4). See "Report-timezone capture & the time-context contract" below.

### Full-grain preserved separately from the canonical projection (Story 12.4)

A source's FULL grain (every selected dimension) is preserved in a SEPARATE
relation (`candidate_full_grain`) — each dimension its own typed column, a
`grain_key`, all source fields, and provenance. It is NEVER projected directly
into `fact_daily_kpi`: only governed additive measures (`aggregation='sum'` AND
`non_additive=false` via `app.mdm_canonical_fields`) plus ONE governed dimension
projection reach the canonical fact. A non-additive measure (ratios,
`average_position`) is routed to the `semantic_*` VIEW pattern, NEVER stored as an
additive fact. The compile gate lives in `server/core/datastream_projection.py`;
the mart-side hard guard is `test_projection_additive_only.sql` /
`fact_kpi_metric_additive_only`.

### Byte-identical totals / hero-KPIs / canonical-partition — a review requirement

Any change that adds a mart block or a projection dimension MUST leave existing
per-connector totals, hero-KPI additive totals (`compute_rollup`), source-isolated
values, and the canonical single-dimension partition selection
(`MIN(breakdown_dimension)` / `rollup.canonical_breakdown_per_connector`)
**byte-identical**. A new dimension must be a PARALLEL series that `MIN` / the
Python selector never pin as canonical (it must sort AFTER the canonical
dimension), and a top-N-bounded partition must be proven `<=` the full-coverage
canonical total. Prove it with the `test_*_totals_isolated.sql` /
`test_*_min_breakdown_stable.sql` idiom (zero rows = pass) plus a
`compute_rollup` invariance case. Reviewers must reject any change that cannot
show this proof (the Epic-10 CRITICAL-A ×N inflation lesson).

### Money adapter & the canonical-micros contract (Story 39.2, E39-AD1)

Money is normalized to ONE canonical internal representation — **micros, currency
attached — for everyone** — by a per-source **money adapter**, so heterogeneous
source money is **summable** (one exact integer unit) and **comparable**
(currency-tagged; FX is a later concern, 39.3/39.4).

- **Declare, don't hard-code.** A connector declares its NATIVE money encoding
  (`native_unit ∈ {micros, decimal, cents}` + the currency locus) as an additive
  `money` sub-object on its `source_capabilities` field (the adapter's INPUT
  contract, diffable against the API doc). The canonical map the read layer keys on
  is the projection seed `dbt/seeds/money_metric_units.csv` (one row per **monetary**
  canonical metric — 39.1's `monetary:true` set — mapping it to its native unit).
- **The adapter normalizes to canonical micros.** The platform adapter
  (`server/core/money.py:to_canonical_micros`) converts native → canonical micros
  (integer-exact) at the capture/staging boundary. The adapter OUTPUT is **always**
  canonical micros; `micros` is a no-op, `decimal` is `×1e6`, `cents` is `×10_000`.
  An unknown native unit **fails closed** (never guesses a scale).
- **Divide once at read.** The `/1e6` back to display units happens **exactly once**,
  at read — `core.money.read_units` (Python) and the semantic ratio views (SQL) both
  read the SAME seed. Dividing per row accumulates float drift over a SUM; keeping the
  micros total integer-exact and dividing once is exact.
- **Ratios reconstructed at view, per-component unit-normalized (AD-4 / NFR01).**
  eCPM/CPC/CPA/ROAS = SUM(numerator) / SUM(denominator), where each **monetary**
  component is `/1e6` (view-level, OUTSIDE the SUM) **iff** it is declared canonical
  micros. Never divide-then-sum; never store a pre-divided ratio.
- **Byte-identical totals covers money.** Adding the adapter declaration + the
  micros-aware views must leave every already-correct single-currency total
  **byte-identical** (E39-NFR06) — proven by a real `dbt build`, drift threshold
  exactly `0`. The adapter is declared + unit-tested now; a connector's staging only
  routes through it when that connector adopts it (its own story), so incumbent
  decimal marts stay byte-for-byte as they are.

### Report-timezone capture & the time-context contract (Story 39.7, E39-NFR02)

Every **daily** connector captures the exact **report timezone** the source used to draw
its reporting-day boundaries as **per-row provenance** — the time-context sibling of the
money adapter, generalizing GAM's existing `report_timezone` capture into one contract:

- **Declare `time_context`, don't hard-code.** A connector declares WHERE it reads its
  report timezone as an additive `time_context` sub-object on its `source_capabilities`
  **descriptor** (`{locus ∈ {network,property,account,fixed,none}, fallback ∈ {gap,assume},
  fixed_zone?, assumed_zone?}`). It sits on the DESCRIPTOR (one report timezone per
  datastream), not on a field (contrast the per-field `money`). `locus` is an ABSTRACT
  source of the zone — never a provider field name (AD-2); the connector reads its OWN
  provider field and hands core only the captured zone string.
- **Core validates + resolves, never guesses.** `server/core/report_timezone.py`
  (`resolve_capture`) validates the captured zone against the stdlib `zoneinfo` IANA
  database and applies the declared fallback posture. The report-timezone provenance column
  (`report_timezone VARCHAR`) is landed per raw row and passed through staging UNCHANGED
  (immutable provenance, E39-AD2) — **never** used to `convert_timezone()` at day grain
  (dishonest at DATE grain; HG-4).
- **Undetermined = a recorded assumption or a `TIMEZONE_GAP`, never a silent zone.** When
  the zone cannot be determined, the contract fails **closed**: either an explicitly-declared
  `assumed_zone` recorded **tagged `assumed=true`** in provenance (`fallback='assume'`), or a
  typed `TIMEZONE_GAP` at the datamodel layer (`_detect_conflicts`, shape-aligned with
  `CURRENCY_GAP`, `resolvable_via='source_timezone_declaration'`). There is **no code path
  that silently writes `'UTC'` or the project default** (E39-NFR02). Absent declaration =
  today's behaviour; adding the capture column must leave every existing total
  **byte-identical** (E39-NFR06). Signalling cross-source day-offsets is Story 39.8 (which
  DEPENDS on this capture); 39.7 is CAPTURE only.

### Publication = pointer swap over already-validated data (Story 12.5)

Publishing a candidate is a **pointer swap over already-validated data — there is
NO recomputation at publish time.** `commit_publication`
(`server/core/datastream_publication.py`) promotes a compiled, DQ-gated candidate:
it advances the execution to `published`, writes the append-only publication log,
swaps `app.datastreams.current_published_execution_id`, and enqueues the outbox
event — **all four writes in ONE Postgres transaction**. The content hash is
validated BEFORE the swap; provenance columns (`execution_id`,
`mapping_version_id`, `plan_version_id`) are EXCLUDED from the hash, so the 12.5
provenance backfill never changes a published total. Any failure rolls back the
whole group leaving the prior published pointer intact, and the execution is
marked `failed` in a **separate** connection (never the rolled-back one).

`app.datastream_publication_log` is **append-only** (an immutability trigger
mirroring `trg_datastream_plan_versions_immutable` rejects UPDATE/DELETE). A
rollback (Story 12.12) is a **new log row** pointing the pointer back at a prior
`dse_<ULID>` — never a mutation of existing history. DQ gates (empty candidate,
row-count delta, content-hash match, schema-hash drift) are **project-preference
governed** (`max_row_count_delta_pct`, `allow_empty_publication` on
`app.project_preferences`) and **fail closed**: a gate failure leaves the prior
published execution current.

## Ajouter un connecteur

Pour ajouter un nouveau connecteur / source de données, voir **`docs/adding-a-connector.md`** —
le guide complet : socle commun hérité, choix du landing kind (kpi/context/generic), étapes
numérotées avec snippets exacts, checklist finale, et pièges connus.

Raccourci via le skill Claude Code : `/add-connector <nom> <kind>`

## Schema Change Checklist

Derived from Epic 4 retrospective action item **AI-29**. Any change that alters a
persisted or wire schema MUST walk this checklist before code-review sign-off. A
"schema" here means any of: a Postgres migration (`infra/nango/migrations/`), the
canonical envelope schema (`server/tests/conformance/schemas/envelope.schema.json`),
the manifest and source capability schemas (`server/core/schemas/manifest.schema.json`,
`server/core/schemas/source-capabilities.schema.json`), a dbt model grain,
or a conformance golden fixture.

- [ ] **Migration is additive & idempotent.** New columns are `NULL`-able (or have a
      default) and use `IF NOT EXISTS` / `IF EXISTS`. No destructive `DROP`/`ALTER
      TYPE` on a populated column without an explicit backfill + rollback note. Files
      follow the sequential `NNN_description.sql` naming in `infra/nango/migrations/`
      (e.g., `032_datastream_field_mappings.sql`; confirm the next free ordinal at
      merge time — the number may be provisional while parallel branches are unmerged).
- [ ] **Protect immutable version contracts.** Append-only tables need database
      enforcement for both `UPDATE` and `DELETE`, unique per-parent ordinals,
      same-scope composite foreign keys, deterministic content hashes, and a
      live-Postgres constraint test. Cleanup for those tests must use transaction
      rollback rather than deleting immutable evidence.
- [ ] **Distinguish governed-mutable registries from append-only versions.** A
      governed identity/registry table (e.g. `app.mdm_canonical_fields`) is
      mutable-with-`updated_at` + a same-connection audit row, NOT append-only —
      do NOT attach an immutability trigger to it. Enforce scope uniqueness with
      partial unique indexes when a nullable scope column (`project_id IS NULL` =
      platform scope) participates, add row-local CHECKs (e.g. a metric must
      declare an aggregation or be flagged non-additive), and prove the FKs
      resolving against it fail closed (a missing/archived/out-of-scope target
      blocks, never silently passes). The resolving write path stays read-only
      against the registry.
- [ ] **Validate Ossie semantic projections.** Any mapping version projection MUST
      validate against the pinned local Ossie profile schema (`server/core/schemas/ossie-0.1.1-profile.schema.json`),
      set `ossie_spec_version="0.1.1"`, and carry all toorow-specific facts under
      `custom_extensions[vendor_name="toorow"]` without modifying Ossie core keys.
- [ ] **Prove time-window semantics.** Any scheduled schema stores an IANA timezone
      and defines whether intervals are inclusive or half-open. Test consecutive
      windows in UTC, both DST transitions, and a non-whole-hour-offset zone; keep
      watermark delay and intentional late-arrival overlap separate from base
      interval ownership.
- [ ] **Prove atomic audit evidence.** A mutation that promises atomic state and
      audit must insert the audit row through the caller's database connection and
      transaction. Test that an audit failure rolls back the version append, mutable
      state, and current pointer together; a best-effort second connection is not
      acceptable evidence.
- [ ] **Prove mutation idempotency under locking.** Hash but never store or return raw
      idempotency keys. Lock the owning aggregate before ordinal allocation and retry
      lookup; test same-key replay, changed-payload conflict, cross-object key reuse,
      and concurrent revisions.- [ ] **Regenerate golden fixtures.** If the change touches the envelope, manifest, or
      any pull output shape, regenerate the affected conformance fixtures under
      `server/tests/conformance/fixtures/` (and any module golden fixtures) and commit
      them in the same change. Never hand-edit a fixture to make a test pass — the
      fixture is the contract; regenerate it from the producing code.
- [ ] **Envelope additive-under-1.x policy.** New `meta` keys are additive and keep
      `schema_version` unchanged within a 1.x line (see the policy note in
      `envelope.schema.json`). A breaking change (removed/renamed/retyped key) bumps
      the major schema version and updates every consumer.
- [ ] **Declare new keys in the schema.** Any new `meta`/`data` key surfaced by a tool
      is declared in `envelope.schema.json` so the conformance envelope layer validates
      it (Layer 2). Undeclared keys are a review failure.
- [ ] **Preserve the capability contract.** A built-in manifest at schema `1.2`
      has one strict `source_capabilities` descriptor. Keep `report_profiles`
      aligned, declare exact source/canonical field identity and grain, and make
      every profile either selectable with its exact `dispatch.callable` or
      unavailable with a stable reason and follow-up. Explicit profile dispatch
      never falls back to `pull()`.
- [ ] **Run the all-module capability gate.**
      `uv run pytest server/tests/conformance/test_all_module_capabilities.py -q`
      must pass independently of the per-module conformance command.
- [ ] **Update the grain test.** A dbt grain change updates the `<model>_grain_unique`
      test (see AI-05 above) to name every identifying column.
- [ ] **Run the gates.** `uv run ruff check server` and `uv run pytest server/tests`
      pass, plus the conformance suite for each affected module
      (`uv run pytest server/tests/conformance --module-path server/modules/<name>`).

## Integration Test Requirements

Every new `INSERT` path that writes to a table with `NOT NULL`, `FK`, or `UNIQUE`
constraints must have at least one integration test running against a real Postgres
instance (or an in-memory Postgres started via a pytest fixture — not a mock cursor).

Mocked DB tests verify business logic branches. They cannot substitute for schema
constraint verification — the Epic 5 anomaly-firing F-1 bug (missing project_id
NOT NULL violation masked by mocks) demonstrated this failure mode.

The `app.project_reports` INSERT path introduced in Story 6.1 is the first to follow
this rule. Use the `live_postgres` pytest fixture (add if not existing) to verify the
UNIQUE (project_id, module_name, report_id) constraint is enforced.

### AI-45 — Derived-SQL seam tests (multi-range mandatory)

Every endpoint whose response is derived from a SQL query over multi-row/multi-range
warehouse data (extract ledger, report chain, DQ issues, and any future endpoint of
this shape) MUST have at least one test in
`server/tests/integration/test_derived_sql_seams.py` (or a dedicated `test_*_seams.py`)
that exercises the full `build_asgi_app()` ASGI stack against the `live_postgres`
fixture with MULTI-DAY, MULTI-RANGE seed data (contiguous + gap + overlapping ranges).
Single-day or single-row seed data is forbidden as the sole coverage for derived-SQL
logic: it trivially satisfies both the correct and the buggy version of most range
predicates — the Epic 8 ledger CRITICAL passed 37 single-day tests while the multi-day
SQL was completely broken. Assert on derived VALUES (statuses, counts, range
membership), never just HTTP 200. Tests auto-skip when `TEST_POSTGRES_DSN` is unset.

### AI-54 — Fixtures = real server payload

A UI widget fixture mirrors what the server ACTUALLY emits, never the spec's intended
shape. If the spec wants a shape the server cannot serve yet (missing ingestion,
missing resolver), the gap goes to `deferred-work.md` — not into the fixture. An
aspirational fixture makes the dev build lie (lesson: Epic 9 usertypes donut rendered
a `user_type` dimension the warehouse has never carried).

### AI-54 — Written envelope contract before parallel server/UI dev

When a server agent and a UI agent build the two sides of a payload in parallel, the
orchestrator writes the EXACT envelope contract (block types, field names, French
label values) into BOTH prompts before launch. Where Epic 9 did this (connectors
card) the two sides met with a single accent diff; where it did not (movers bars,
status labels), it cost three alignment round-trips.

## Running the Isolation Suite (Story 7.4 — FR12 / AD-5)

The isolation suite (`server/tests/isolation/`) turns "no query crosses project
scope" from a promise into a **verified property**. It provisions two isolated
projects with distinct data across the whole AD-5 addressing tree
(Project → Tool → Auth → Report → Dimension) and asserts that a request scoped to
one project never returns the other's data — including cross-scope admin attempts,
which must be **rejected (404) AND audited** (`action = access_denied`).

The suite requires a live Postgres with all app-schema migrations applied
(`001`–`021`, including `021_project_members.sql`). It is `@pytest.mark.isolation`
and **skips** cleanly when `TEST_POSTGRES_DSN` is unset — so the default local and
CI test runs are unaffected.

Run it locally against your dev Postgres:

```bash
# Apply migration 021 first (adds app.project_members + relaxes audit_log FK):
docker compose -f infra/nango/docker-compose.yml exec platform-db \
  psql -U connector -d connector -f /migrations/021_project_members.sql

# Then run the suite (both DSN vars point the fixtures + audit writer at the DB):
export TEST_POSTGRES_DSN='postgresql://connector:connector_dev_only@localhost:5432/connector'
export PLATFORM_DB_URL="$TEST_POSTGRES_DSN"
export HEALTH_POLLER_ENABLED=false QUEUE_WORKER_ENABLED=false SCHEDULER_ENABLED=false
export TOOROW_AUTH_MODE=disabled
uv run pytest server/tests/isolation/ -v -m isolation
```

**Permanent CI gate:** the `isolation` job in `.github/workflows/ci.yml` runs this
suite against an ephemeral Postgres service on every push and PR. A failing
isolation test **fails the build and blocks merge** — isolation regressions cannot
land silently.

### Per-identity project ACL (`app.project_members`)

Migration 021 introduces `app.project_members (identity, project_id, role)` with a
**default-open** fallback: a project with **zero** membership rows is reachable by
every identity (single-tenant compat — existing tests stay green); the moment a
project gains its first member row it is **closed** to non-members. The enforcement
point is `core.project_access.identity_has_project_access` (wired into
`_assert_project_access` and the notebook write handlers). At P3-dev the admin API
still uses a shared Bearer token, so cross-scope tests simulate a second identity by
passing an explicit wrong `project_id` scope — the endpoint validates ownership
regardless of credential.

---

## Exploitation locale (runbook)

Procédures opérationnelles pour le déploiement P3-dev sur poste Windows ou Linux.

### A. Sauvegardes

**Dumps Postgres quotidiens** (deux conteneurs) :

```bash
# Base plateforme (connector)
docker exec nango-platform-db-1 pg_dump -U connector connector > backup_connector_$(date +%Y%m%d).sql

# Base Nango interne
docker exec nango-nango-db-1 pg_dump -U nango nango > backup_nango_$(date +%Y%m%d).sql
```

**Clés de chiffrement** (`infra/keys/`) — archiver chiffre :

```bash
# Windows (7-Zip)
7z a -p infra_keys_$(date +%Y%m%d).7z infra/keys/

# Linux
7z a -p infra_keys_$(date +%Y%m%d).7z infra/keys/
# ou : tar czf - infra/keys/ | openssl enc -aes-256-cbc -out infra_keys_$(date +%Y%m%d).tar.gz.enc
```

**Fichiers DuckDB** (seeds locaux) :

```bash
cp server/modules/google-analytics/seeds/local.duckdb backups/local_$(date +%Y%m%d).duckdb
```

**Procedure de restauration Postgres** :

```bash
docker exec -i nango-platform-db-1 psql -U connector connector < backup_connector_20260712.sql
```

> **AVERTISSEMENT** : `docker compose down -v` detruit tous les volumes Docker
> (donnees Postgres incluses). Ne jamais lancer cette commande sans sauvegarde
> prealable. Utiliser `docker compose down` (sans `-v`) pour un arret simple.

---

### B. Machine neuve — bootstrap complet

Ordre obligatoire :

```bash
# 1. Demarrer les services (Nango + Postgres + plateforme)
docker compose -f infra/nango/docker-compose.yml up -d

# 2. Appliquer les migrations 001-021+ dans l'ordre
for f in $(ls infra/nango/migrations/*.sql | sort -V); do
  echo "application de $f"
  docker exec -i nango-platform-db-1 \
    psql -U connector -d connector -v ON_ERROR_STOP=1 -f /dev/stdin < "$f"
done

# 3. Seeder et construire les modeles dbt
cd dbt
uv run dbt seed --profiles-dir profiles
uv run dbt build --profiles-dir profiles
cd ..

# 4. Installer les dependances Python
uv sync --all-packages

# 5. Copier et remplir le fichier .env
cp .env.example .env
# Editer .env : remplacer TOOROW_STATIC_TOKEN par un token aleatoire,
# renseigner NANGO_ENCRYPTION_KEY, PLATFORM_DB_URL, etc.
```

---

### C. Serveur persistant sous Windows (survie aux redemarrages)

**Option 1 — NSSM (recommande)** :

```
# Installer NSSM depuis https://nssm.cc/download puis :
nssm install toorow "C:\path\to\.venv\Scripts\python.exe" "-m core.main"
nssm set toorow AppDirectory "C:\Users\littl\Programmation\connector"
nssm set toorow AppEnvironmentExtra "PORT=8000" "TOOROW_AUTH_MODE=static"
nssm set toorow AppStdout "C:\logs\toorow.log"
nssm set toorow AppStderr "C:\logs\toorow-err.log"
nssm start toorow
```

**Option 2 — Task Scheduler (`schtasks`)** :

```cmd
schtasks /create /tn "ConnectorAtlas" /tr "C:\path\to\.venv\Scripts\python.exe -m core.main" ^
  /sc ONSTART /ru SYSTEM /rl HIGHEST /f
```

Les logs uvicorn vont dans `AppStdout` (NSSM) ou dans la sortie standard capturee
par Task Scheduler (configurable via `schtasks /create ... /rl HIGHEST`).
Pour NSSM, consulter les logs via `type C:\logs\toorow.log`.

---

### D. Activer la livraison des alertes

1. Configurer le serveur SMTP dans `.env` :

```bash
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=toorow@example.com
SMTP_PASSWORD=<mot-de-passe>
ALERT_EMAIL_TO=jean@example.com
ALERT_EMAIL_FROM=toorow@example.com
ALERT_EMAIL_ENABLED=true
```

2. Activer les alertes et le planificateur :

```bash
ALERTS_ENABLED=true
SCHEDULER_ENABLED=true
SCHEDULER_TIMEZONE=Europe/Paris
```

3. Pour les alertes metier et anomalies (optionnel) :

```bash
BUSINESS_ALERTS_ENABLED=true
ANOMALY_ALERTS_ENABLED=true
```

La section **Observability signals** (`AI-32/AI-41`) dans les conventions
ci-dessus documente les types de firings ecrits dans `app.alert_firings`
(`scheduler_step_degraded`, `nango_revoke_failed`, etc.) et leurs champs de
metadonnees. Pour SMTP local en dev, utiliser Mailpit :

```bash
docker run -p 1025:1025 -p 8025:8025 axllent/mailpit
# puis SMTP_HOST=localhost SMTP_PORT=1025 (sans auth, sans TLS)
```
