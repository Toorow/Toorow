# toorow — MCP core (kernel)

The core is the microkernel (AD-2). It knows nothing about any specific
marketing platform; all platform knowledge lives in `server/modules/<name>/`
and is composed via FastMCP `mount()` under the module's namespace. Story 1.1
scaffolds only the skeleton and a `health` tool.

## Transport — streamable HTTP (T3.1)

The server runs FastMCP **3.4.x** over **streamable HTTP** (not stdio, not
WebSocket), per ARCHITECTURE-SPINE §Deployment and SOLUTION-DESIGN §4.

- The ASGI app is built via `mcp.http_app(transport="streamable-http")`,
  mounted at `/mcp`.
- It binds `0.0.0.0:$PORT` — `PORT` is injected by Cloud Run; default `8000`
  for local dev.
- `python -m core.main` starts it under uvicorn.

Endpoint: `http://<host>:<PORT>/mcp`

## Host-header handling / 421 Misdirected Request (T3.2)

Cloud Run routes by `Host` header. A request whose `Host` does not match the
service hostname must be answered with **421 Misdirected Request** (also the
MCP DNS-rebinding protection posture).

Two complementary mechanisms are in place:

1. **FastMCP native guard (production default).** FastMCP 3.4.3+ ships a
   built-in Host/Origin guard that returns 421 on a disallowed Host. Configure
   it via environment variables:

   | Env var | Meaning |
   |---|---|
   | `FASTMCP_HTTP_HOST_ORIGIN_PROTECTION` | `true`/`false` — enable the native guard |
   | `FASTMCP_HTTP_ALLOWED_HOSTS` | comma/JSON list of allowed `host[:port]` |

   > ⚠️ With `FASTMCP_HTTP_ALLOWED_HOSTS` empty, the effective allowlist is
   > empty and **every** Host fails. On Cloud Run you MUST set it to the
   > service hostname (e.g. `mcp-server-xxxx-uc.a.run.app`).

2. **Story-contract ASGI middleware (explicit + testable).**
   `HostHeaderValidationMiddleware` in `main.py` honours the Story-1.1 knobs:

   | Env var | Meaning |
   |---|---|
   | `HOST_HEADER_VALIDATION` | `strict` to enable; anything else = off |
   | `ALLOWED_HOST` | comma-separated allowed `host[:port]` values |

   When strict and the inbound `Host` is not allowed it returns 421 with the
   canonical error body `{"code","message","provenance"}`. This layer is
   version-independent and is covered by `tests/test_health.py`.

Local dev leaves both guards **off** (no env vars set) so `curl`/MCP clients on
`localhost` work without configuration.

> **Cloud Run activation:** See `infra/docs/cloud-run-host-guard.md` for the
> step-by-step procedure to retrieve the assigned `.run.app` hostname after
> first deploy and set `FASTMCP_HTTP_ALLOWED_HOSTS` / `ALLOWED_HOST` in the
> GitHub Actions Variables (Story 2.1 AC6).

## AD-1 response envelope

Every data-returning tool returns the canonical `structuredContent` envelope:

```json
{
  "schema_version": "1",
  "meta": { "freshness": "...", "provenance": {...}, "alerts": [] },
  "data": { ... }
}
```

## AD-14 identity scaffold

`health(project_id="default")` carries the identity parameter from P0. Real
resolution (OAuth 2.1 + PKCE) arrives in Story 2.3; the parameter is never
bolted on later.

## Error shape

Tool errors return `{"code","message","provenance"}` with `isError: true`
(ARCHITECTURE-SPINE §Consistency). The reserved code `auth_expired` triggers the
shared shell's reconnect affordance.

## Dual-channel pattern for data tools (AD-1 / NFR1) — REQUIRED from Story 1.5

A dict return surfaces as `structuredContent` ONLY — FastMCP does not
synthesize a text channel from the docstring. Data-returning tools must
explicitly emit BOTH channels:

```python
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

@mcp.tool
def get_daily_report(project: str, date_range: str) -> ToolResult:
    envelope = _envelope(full_dataset, provenance=...)   # multi-MB is fine here
    summary = build_rollup_summary(envelope)             # <=30 lines, ~500 tokens
    return ToolResult(
        content=[TextContent(type="text", text=summary)],  # LLM channel
        structured_content=envelope,                       # widget channel
    )
```

Never return the raw envelope alone from a data tool: the whole dataset would
enter the LLM context and violate NFR1 (see review-1-1.md, finding M2).

### Auth Modes

The server reads `TOOROW_AUTH_MODE` at startup to configure inbound request
authentication (Story 2.3, AD-14).

| `TOOROW_AUTH_MODE` | When to use | Auth requirement |
|---|---|---|
| `disabled` (default) | In-process tests, CI, local dev | No token required |
| `static` | Manual QA, integration test against a running server | Static `TOOROW_STATIC_TOKEN` |
| `oauth` | Cloud Run production | JWT from external IdP (RS256) |

**Disabled mode (default for tests/local dev):**
No env var needed. All existing tests use `FastMCPTransport` (in-process) with
no auth. `TOOROW_AUTH_MODE` unset or `disabled` produces this mode.

**Static mode (manual QA):**
```
TOOROW_AUTH_MODE=static
TOOROW_STATIC_TOKEN=<your-bearer-token>
TOOROW_STATIC_SUBJECT=<identity-subject>   # default: "default-user"
```
Present `Authorization: Bearer <token>` with the configured token value.

**OAuth mode (cloud/production):**
```
TOOROW_AUTH_MODE=oauth
TOOROW_JWT_PUBLIC_KEY=<PEM-string>    # OR TOOROW_JWKS_URI
TOOROW_JWT_ISSUER=<issuer-url>        # required
TOOROW_JWT_AUDIENCE=<api-audience>    # required
```
Set exactly one of `TOOROW_JWT_PUBLIC_KEY` or `TOOROW_JWKS_URI`.
Literal `\n` in the PEM string is auto-converted to actual newlines when
reading from `.env` files or shell exports.


Browser authentication is a separate BFF contract. A self-hosted console uses
`TOOROW_BROWSER_AUTH_MODE=oidc`, Authorization Code + PKCE and an encrypted
HttpOnly session cookie; provider tokens never enter JavaScript. The ID-token
audience is the explicit `TOOROW_OIDC_CLIENT_ID`, not the API Bearer audience
above. Protected new setups also require `TOOROW_CANONICAL_IDENTITY_ENABLED=1`.
See `infra/docs/self-hosting.md` and `.env.example` for the complete environment
and CSRF/public-Origin contract.
**Identity in tool responses:**
All core tools (`health`, `list_modules`, `get_daily_report`) include
`data.identity` in their response envelopes. In disabled mode this is
`"anonymous"`. In static/oauth mode it is the `sub` claim from the token
(or `client_id` as fallback). Epic 7 will add the full identity-to-project
ACL mapping; the field is present now per AD-14.

## Tool naming convention (normative — AD-2)

Module tools mount namespaced with an UNDERSCORE separator between the
manifest `name` (kebab-case, pattern `^[a-z0-9-]+$`) and the tool name:

    <module-name>_<tool_name>      e.g.  my-module_get_report

This is FastMCP 3.4's `Namespace` transform behavior. Widgets and core code
that reference module tools (Stories 1.5/1.6+) MUST use this exact shape.
Core-owned cross-connector tools (`get_daily_report`, `submit_feedback`,
`list_modules`, `health`) are never namespaced.

Manifest schema evolution: `schema_version` accepts `1` or `1.x`; unknown
top-level manifest keys are ALLOWED (ignored by core) so newer modules can
declare fields that older cores don't understand yet (review-1-3 M1). Version
`1.2` requires the strict `source_capabilities` descriptor. Older manifests may
still execute for compatibility, but the loader marks their capability catalog
unavailable and never infers metadata from legacy fields.

## Governed source capability catalog (schema 1.2)

`core/source_capabilities.py` owns generic structural/semantic validation,
deterministic allow-list normalization, and project/connection scoping. Provider
vocabulary remains in `server/modules/<name>/manifest.json`; core never serializes
a raw manifest.

The same service feeds both public adapters:

- `GET /api/source-capabilities?project_id=<id>&connection_ref_id=<id>`
- MCP `get_source_capabilities(project_id, connection_ref_id)`

The MCP result uses `ToolResult`: a short report-count summary in `content` and
the complete normalized catalog in `structured_content`. `list_modules` remains
a lightweight compatibility index and returns only sanitized profile summaries.

Scope is fail-closed for this metadata surface. REST returns `400 invalid_input`,
`401 unauthorized`, a non-disclosing `404 source_capabilities_not_found` for
unknown/cross-project/inactive/disabled/mismatched resources, or
`503 source_capabilities_unavailable` when access or descriptor state cannot be
proven. MCP uses the corresponding stable codes `invalid_input`,
`source_capabilities_not_found`, and `source_capabilities_unavailable` with
`is_error=true`.

Explicit profile execution is also fail-closed: `get_module_pull_fn(module,
profile_id)` resolves only the exact `dispatch.callable` of a selectable
capability report. Unknown or unavailable profiles return no callable. Only
`profile_id=None` retains the legacy default `pull()` path.

Repository gate:

```bash
uv run pytest server/tests/conformance/test_all_module_capabilities.py -q
```

This gate validates all built-ins, semantic references, normalized secrecy, and
the invariant that every declared profile is either backed by its declared
callable or explicitly unavailable with a reason and follow-up.

## Versioned Datastream intent contract (Story 12.2)

`core/datastream_intents.py` is the single persistence and validation path for
versioned Datastream drafts. A save appends an immutable `dsp_<ULID>` plan,
updates the Datastream current-plan pointer and exact-plan schedule state, and
writes its audit row in one PostgreSQL transaction. Saving or validating a draft
always leaves the Datastream disabled; execution starts in Story 12.6.

| Source kind | Writer | Destination | Ownership rule |
|---|---|---|---|
| `connector_pull` | `toorow` | `managed_raw` | opaque project-scoped connection and report |
| `external_bq` | `external` | `external_read_only` | declared external object and writer; no Connector credential |
| `managed_feed` | `toorow` | `managed_raw` | managed immutable feed candidates; writing is a later story |

Writer kind is derived from source kind. The strict Draft 2020-12 schema rejects
unknown keys recursively, contradictory ownership, raw credentials, tokens, and
arbitrary config. A structurally safe incomplete selection is saved with
`executable=false` and deterministic repair issues.

The schedule vocabulary is `manual`, `daily`, or `hourly`. Canonical source
ownership uses aware UTC half-open intervals `[window_start, window_end)`.
`historical`, base interval, watermark delay, late-arrival lookback, retry, and
missed-run coalescing remain separate values. Daily boundaries use the saved IANA
timezone; the DST transition can therefore produce a 23- or 25-hour UTC interval
without creating a gap or duplicate ownership.

Version-creating REST mutations require `Idempotency-Key`. The server stores and
audits only its SHA-256 hash. A same-project retry for the same Datastream and
normalized payload returns the original version; payload or Datastream reuse with
the same key returns `409 idempotency_conflict`. `SELECT ... FOR UPDATE` serializes
version-number allocation before the retry check.

| Operation | Minimum role |
|---|---|
| list/detail/version history | Viewer |
| validate/create/revise/run/refetch policy | Member |
| external read-only ownership or deletion | Owner |

These Datastream endpoints use the strict role resolver: production membership is
default-deny and returns a non-disclosing 404. Only disabled-auth local development
maps `anonymous` to Owner. The schema-v2 Flow/MCP facade delegates to the same domain
service and the same strict role path.

Legacy rows with no current plan remain readable and keep `nightly` as a daily read
alias. Their enabled state and legacy scheduler behavior are unchanged. A versioned
row is disabled and excluded from that scheduler; it is soft-archived on deletion.
The legacy queue currently deduplicates active jobs by connection and inclusive date
range. Story 12.6 must include Datastream and exact plan version in that key before
versioned dispatch, so two report profiles cannot suppress each other.

Story 12.2 performs metadata validation and PostgreSQL writes only. It creates no
provider, BigQuery, or Google Sheets API integration, so AI-13 is not applicable.
Live service evidence belongs to the first story that calls each external service.

## Versioned Datastream field mapping & Ossie 0.1.1 projection contract (Story 12.3)

`core/datastream_field_mapping.py` owns physical profiling, semantic classification, ambiguity detection, mapping version persistence, and Apache Ossie 0.1.1 projection generation.

- **Suggestion ≠ decision:** Profiling produces `suggestion` records (`status="suggested"`, `confidence`, `evidence`). Low-confidence (below the configured threshold, `0.75` default) or conflicting bindings stay `blocking` and `executable=false` until explicitly confirmed via `confirm_binding()` with an actor and reason.
- **Derived confidence:** `confidence` is derived from profiling signals — physical-type certainty, kind alignment, semantic-hint presence, and sample/evidence sufficiency — not a hardcoded constant. An unrecognized physical type is capped low so it stays blocking by default.
- **Ambiguities:** Required ambiguities are surfaced as structured records with deterministic repairs: `multiple_date_candidates`, `mixed_grain` (date candidates at differing granularity), `unknown_field` (unrecognized physical type/kind), plus conflict codes reused verbatim from `datamodel._detect_conflicts` — `CURRENCY_CONFLICT` (currency/unit ambiguity on a measure) and `MEASURE_NULL` (a non-additive measure bound additively). 12.3 records conflict state; Epic 13 resolves it.
- **Immutability & drift:** Mapping versions (`dmap_<ULID>`) are monotonically versioned per Datastream, content-hashed, append-only, and pointer-swapped atomically in `app.datastreams.current_mapping_version_id`. They record `source_schema_hash` and `capability_fingerprint`; drift invalidates execution eligibility.
- **MDM canonical-identifier registry:** 12.3 CREATES `app.mdm_canonical_fields` (`mdm_<ULID>` id, AD-5 nullable `project_id` scope, `concept_kind`, `canonical_name` unique per scope via two partial indexes, unit/currency scope, aggregation/`non_additive` with a metric-must-declare-aggregation CHECK, optional `dictionary_field_name` FK to `app.target_fields`). Registry rows are mutable-with-audit (governed by Epic 13), NOT append-only. A binding's `mdm_target` FK-references this registry: `save_field_mapping()` resolves it with a scoped read (`project_id IS NULL OR project_id = <project>`) and marks a binding `blocking` with `blocking_reason="register_or_pick_canonical_field"` when the id is missing/archived/out-of-scope. The save path is read-only against the registry (writes zero rows); an empty/unreadable registry fails closed (mdm-bound bindings block).
- **Pinned Ossie 0.1.1 projection:** Deterministically projects onto pinned Apache Ossie 0.1.1 concepts (`ossie_spec_version = "0.1.1"`): `semantic_model` root containing `datasets`, `relationships`, and `metrics`. All toorow-specific facts (physical type, aggregation, non-additive flag, sensitivity, confidence, canonical/MDM targets) live strictly inside Ossie's native `custom_extensions` array as `{"vendor_name": "toorow", "data": <json-string>}` without redefining Ossie core keys.
- **Scope boundary:** 12.3 owns classification, mapping, the MDM registry table + the binding FK, and Ossie projection ONLY. It does not own Epic-13 registry/dictionary approval/conflict-resolution governance (it records conflict state and blocks unresolved bindings; it never approves, resolves, or edits governed rows), 12.4 full-grain dataset materialization, 12.5 publication, or UI components.

## Safe KPI projection compile contract (Story 12.4)

`core/datastream_projection.py` is the sole owner of the pure, source-agnostic
safe-projection compiler. It consumes ONE immutable 12.3 mapping version and emits
a deterministic projection plan (validated against
`schemas/datastream-projection.schema.json`, Draft 2020-12,
`additionalProperties:false`). Same mapping → byte-identical plan. It writes no
mart (AD-8: dbt is the only mart writer), moves no pointer, calls no provider, and
performs no BigQuery write.

- **Two relations, one truth.** The compiler describes (a) a FULL-GRAIN relation
  that preserves EVERY selected dimension at the declared joint grain (each
  dimension as its own typed column, a deterministic `grain_key`, all source
  fields, and provenance `execution_id`/`pull_id`/`mapping_version_id`/
  `plan_version_id`/`loaded_at`) — materialized by the dbt model
  `candidate_full_grain`, a DISTINCT artifact from `fact_daily_kpi`; and (b) a
  SAFE canonical projection into `fact_daily_kpi`.
- **Accept rules.** A measure reaches `fact_daily_kpi` ONLY iff the mapping
  declares it `aggregation='sum'` AND `non_additive=false` (via
  `app.mdm_canonical_fields`). A dimension reaches it ONLY through EXACTLY ONE
  explicitly governed dimension projection into the canonical
  `breakdown_dimension`/`breakdown_value` slot.
- **Closed reject-code set** (each `{code, path, field_ids, repair}`; any issue
  fails closed and blocks publication): `non_additive_measure_projected` (routed
  to the `semantic_*` VIEW pattern via a `route_to_semantic` repair — NEVER
  summed), `ungoverned_dimension_projection`, `mixed_grain_projection`,
  `cardinality_over_limit`, `scan_over_limit`, `mapping_not_executable`,
  `mapping_drift`. Nothing is silently discarded or coerced (AD-9).
- **Canonical-partition invariance.** A governed projection dimension is a
  PARALLEL series that must sort so `MIN(breakdown_dimension)` /
  `rollup.canonical_breakdown_per_connector` never pin it, keeping every existing
  total and hero KPI (`compute_rollup`) byte-identical (Epic-10 CRITICAL-A ×N +
  CRITICAL-A2 tie-break). Proven by `test_projection_min_breakdown_stable.sql`,
  `test_full_grain_isolated_from_fact_kpi.sql`, `test_projection_additive_only.sql`,
  and the invariance cases in `server/tests/core/test_rollup.py`.
- **Governed cardinality/scan gate.** A metadata-based estimate (no live BigQuery
  dry-run) reads `max_projection_grain_cardinality` /
  `max_projection_scan_bytes` from the project-scoped `app.project_preferences`
  (migration 033), falling back to documented defaults with
  `threshold_source='documented_default'` recorded (no platform-wide hardcode).
  Over-limit names the estimate + the expensive field(s) and BLOCKS, or requires
  the explicit `approved` governed flag — it never silently drops a dimension.
- **REST seam.** `POST /api/datastreams/{id}/projection/compile` (Member;
  `viewer < member < owner`) returns the plan (200) or the deterministic issue
  list (422). It NEVER publishes (publication atomicity is 12.5). Cross-project /
  denied resources return a non-disclosing 404 and are audited.
- **Scope boundary.** 12.4 owns full-grain materialization + safe-projection
  COMPILE-TIME gates ONLY. Not 12.5 publication/active-pointer, not 12.3
  classification/mapping, not 12.6 dispatch.

## Atomic candidate publication contract (Story 12.5)

`core/datastream_publication.py` is the sole owner of the atomic publication step:
the moment a compiled, validated 12.4 candidate becomes the authoritative current
state of a datastream. It is pure, source-agnostic Postgres orchestration (AD-2)
and writes NO BigQuery data (AD-8: dbt owns the mart write; publication is a
pointer swap). Migration `042_datastream_candidate_registry.sql` adds the tables.

- **State machine (typed, closed).** An execution (`dse_<ULID>`) progresses
  `created → loading → validating → ready → publishing → published`, with `→ failed`
  from any non-terminal state and `→ cancelled` from any cancellable
  (non-`publishing`, non-terminal) state. `published`/`failed`/`cancelled` are
  terminal. Every invalid transition is rejected with `invalid_state_transition`.
  `advance_state` writes `state_changed_at` + an actor-stamped audit row per change.
  Transition table (forward edge + the always-available `failed`/`cancelled`):
  - `created → loading` (+ failed, cancelled)
  - `loading → validating` (+ failed, cancelled)
  - `validating → ready` (+ failed, cancelled)
  - `ready → publishing` (+ failed, cancelled)
  - `publishing → published` (+ failed; NO cancel mid-commit)
  - `published` / `failed` / `cancelled` — terminal, no transitions

- **Atomic commit (the 4-write transaction).** `commit_publication` writes, in ONE
  Postgres transaction: (1) execution `ready → publishing → published`, (2) an
  append-only `app.datastream_publication_log` row (rollback evidence via
  `prior_execution_id`), (3) the `app.datastreams.current_published_execution_id`
  pointer swap (pointer row locked `FOR UPDATE` to serialize concurrent publishes),
  (4) an `app.datastream_outbox` `published` event — plus the audit row. Single
  commit; any error rolls back the WHOLE group (prior pointer intact) and marks the
  execution `failed` in a SEPARATE connection (never the rolled-back one).
  Publication is a pointer swap over already-validated data: NO recomputation, NO
  BigQuery write; the content hash is validated BEFORE the swap and excludes
  provenance columns.

- **DQ gate set + preference keys.** `run_dq_gates` fails closed with a closed set
  of `{code, detail, repair}` issues: `empty_candidate` (blocked unless
  `force_empty_publish` AND the `allow_empty_publication` preference),
  `row_count_delta_exceeded` (over `max_row_count_delta_pct`, default 50%, unless
  Owner `approved`), `content_hash_mismatch` (byte-identity, non-overridable),
  `mapping_drift` (`source_schema_hash` ≠ capability fingerprint, non-overridable),
  `schema_hash_mismatch` (landing ≠ plan declared schema). Thresholds are read from
  the project-scoped `app.project_preferences` row; `threshold_source` is recorded
  so the documented-default fallback is never silent.

- **Reconciliation protocol.** `reconcile_execution` (Owner-only, never on the happy
  path) resolves a stuck `publishing` execution from deterministic evidence: pointer
  moved + log row present → resolve to `published`; neither present → resolve to
  `failed` (prior pointer intact). Ambiguous/partial evidence fails closed with
  `reconciliation_inconclusive`. Idempotent: a terminal execution is a no-op.

- **Scope boundary.** 12.5 owns the candidate registry + the atomic pointer swap
  ONLY. 12.4 owns the compile-time gates (an `executable=true` plan is the admission
  ticket); 12.6+ owns provider dispatch and the BigQuery raw landing; 12.11/12.12
  own bounded-reload / rollback (they append new publication-log rows).

## Nightly Scheduler (Story 3.4)

Dispatches nightly pull jobs across a rolling 8-day window for every connection:
- Re-pull window: yesterday-7 through yesterday-1 (7 days -- captures late corrections)
- Fresh window:   yesterday through yesterday (new data)

### Local dev (Windows / Linux / macOS)

Set `SCHEDULER_ENABLED=true` in `.env` to activate the in-process daemon thread.
It checks every 60 seconds and fires at `SCHEDULER_NIGHTLY_HOUR:SCHEDULER_NIGHTLY_MINUTE`
(local time). No cron, Task Scheduler, or external tool required.

| Env var | Default | Description |
|---|---|---|
| `SCHEDULER_ENABLED` | `false` | `true` to activate the thread |
| `SCHEDULER_NIGHTLY_HOUR` | `2` | Local hour to fire (0-23) |
| `SCHEDULER_NIGHTLY_MINUTE` | `0` | Local minute to fire (0-59) |

### CI / unit tests

Leave `SCHEDULER_ENABLED=false` (the default). The scheduler thread MUST NOT
start in tests. All scheduler tests mock the thread or call `dispatch_nightly()`
directly.

### Production (GCP, Phase B)

When `QUEUE_BACKEND=cloud_tasks`, Cloud Scheduler sends a POST to
`/internal/scheduler/dispatch-nightly` on the Cloud Run service. The in-process
thread can remain disabled. Configure:

```
CLOUD_SCHEDULER_JOB_NAME=<name>
CLOUD_SCHEDULER_SERVICE_URL=<Cloud Run URL>/internal/scheduler/dispatch-nightly
```

See `.env.example` for the full Cloud Scheduler variable reference.

## Admin Console (Story 2.4)

The admin console lives at `ui/admin/` and is served by the mcp-server at `/admin`.

### Dev workflow

**Hot-reload (recommended for UI development):**
```
# Terminal 1: start mcp-server (API at :8000)
PORT=8000 uv run python -m core.main

# Terminal 2: start Vite dev server (UI at :5174, proxies /api to :8000)
pnpm --filter @toorow/admin dev
```
Open `http://localhost:5174` in your browser.

**Static-file flow (production-like):**
```
# Build the admin console
pnpm --filter @toorow/admin build

# Start the mcp-server (serves /admin from ui/admin/dist/)
PORT=8000 uv run python -m core.main
```
Open `http://localhost:8000/admin` in your browser.

**Admin API endpoints (served by mcp-server):**
- `GET  /api/connections`  -- list connections (Nango + connection_ref join)
- `POST /api/connections`  -- create a connection_ref row

**Env vars for admin (server-side):**
- `PLATFORM_DB_URL`    -- Postgres DSN (default: dev credentials from docker-compose)
- `ADMIN_DIST_PATH`    -- path to built admin dist/ (default: `ui/admin/dist`)

**Env vars for admin (client-side, in `ui/admin/.env.local`):**
- `VITE_NANGO_PUBLIC_KEY` -- Nango public key (safe to expose to browser)
- `VITE_NANGO_BASE_URL`   -- Nango server URL (default: `http://localhost:3003`)
