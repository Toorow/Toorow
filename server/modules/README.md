# Connector Standard Playbook

This is the operational runbook for bringing ANY connector module to the toorow
industrial standard — whether you are reviewing an existing module one by one or
scaffolding a new one. It is written so that a fresh agent or contributor given
the instruction "bring connector `<name>` to the standard" can execute it
end-to-end without prior context.

Rationale and invariants live in
`_bmad-output/planning-artifacts/epic-25-industrialisation-connecteurs.md`
(the 6-point doctrine, 2026-07-21). This file is the HOW.

## The standard (what a finished module contains)

| Artifact | Purpose | Gate |
|---|---|---|
| `api_catalog.json` | Exhaustive field catalog derived from the OFFICIAL API reference (pinned `api_version`), enriched — never authored by hand | `server/tests/conformance/test_api_catalog.py` (`CATALOG_GATE_MODE=fail`) |
| `catalog_sources.json` | Source declaration: official + enrichment URLs, fetch commands, section→tier map | consumed by the generator |
| `manifest.json` + `error_map` | Provider error codes → canonical error classes | `server/tests/core/test_pull_errors.py` mechanism; module test for the 401 path |
| `manifest.json` + `account_topology` | Account hierarchy + discovery/selection/access-check declaration | core topology tests |
| `connector.py` | Raises typed errors (`core.pull_errors`), no `*_ACCOUNT_ID` env vars, no hardcoded field lists outside the catalog | grep gates + module tests |
| `reports/ratification-<date>.json` | Live-probe evidence against the real API | ONLY thing that lifts `public_catalog.verification: blocked` |

## Step-by-step for one connector

### 1. Identify the sources (read the docs exhaustively — no sampling)

- **Official reference (the authority).** In priority order: a metadata API
  (GA4 Metadata API), a discovery document
  (`https://<service>.googleapis.com/$discovery/rest?version=vN` — full enums,
  machine-readable), or the official field reference pages. Pin the API version.
- **Enrichment (never the authority).** Supermetrics publishes 183 parseable
  catalogs: index `https://docs.supermetrics.com/llms.txt`, one
  `docs/<connector>-fields.md` per source. Known to be INCOMPLETE for some
  providers (their GAM catalog lists ~60% of the real API) — that is why
  enrichment-only fields are reported as suspects, never emitted.
- Also collect the provider's **error code reference** (step 4) and its
  **account hierarchy** model (step 5).

### 2. Generate the catalog

```
# fetch snapshots locally (record the exact commands in catalog_sources.json)
curl -sL <official-url> -o /tmp/<module>-sources/official_fields.json   # or curate it from docs
curl -sL https://docs.supermetrics.com/docs/<x>-fields.md -o /tmp/<module>-sources/supermetrics.md
# copy catalog_sources.json into the sources dir, then:
uv run python scripts/build_api_catalog.py --module <name> --sources-dir /tmp/<module>-sources --report /tmp/report.json
```

- The tool is **local-only** (no network, never in CI). Output is
  byte-deterministic and schema-validated (`core/catalog_contract.py`).
- Read the **fusion report**: `drift_ids` (manifest fields missing from
  official) must end up EMPTY — a non-empty drift means the module currently
  extracts a field the official reference does not list: investigate, never
  ignore. `enrichment_only_ids` are suspects to check against the official doc.
- Commit `api_catalog.json`, `catalog_sources.json` and the curated
  `official_fields.json` snapshot. Do NOT commit the raw Supermetrics markdown.
- Existing manifest fields become `exposure: "exposed"`; the rest is
  `"planned"` — landing the full catalog does NOT require extraction support
  (extraction widens tier by tier in later stories).

### 3. Tier the fields

`section_tier_map` in `catalog_sources.json`: `core` = the fields any user
needs on day one (cost/impressions/clicks/conversions families + structural
ids/names); `standard` = common analysis; `advanced` = the long tail.
Per-field overrides win over the section map. Tiers drive datastream UI
ordering and extraction phasing.

### 4. Map the errors

- Read the provider's error reference (e.g. Meta error codes/subcodes, Google
  `error.errors[].reason`).
- Fill `error_map` in `manifest.json`: `{"<http_status>:<provider_code>":
  "<canonical_class>"}`. Canonical classes: `auth_expired`, `auth_revoked`,
  `permission_denied`, `invalid_request`, `provider_transient` (see
  `server/core/pull_errors.py` — core NEVER hardcodes provider codes).
- The connector's non-429 raise site must be
  `raise classify_http_error(resp.status_code, <parsed body>, error_map)`.
  429 keeps raising `core.quota.RateLimitError` (breaker path).
- Add a module test: a mocked 401 raises `auth_expired` with the provider
  payload preserved (pattern: `server/tests/modules/meta_ads/test_pull_meta.py`).

### 5. Declare the account topology

- `account_topology` in `manifest.json`: the hierarchy (e.g. business → ad
  account; MCC → client account; account → property) and the discovery call
  that lists what the token can reach.
- The connector implements the discovery function; account selection,
  access-check, bounded trial extraction and windowed backfill are core-owned
  flows (`server/core/` topology module) — a pull can NEVER run without a
  selected + verified reporting account. `*_ACCOUNT_ID` env vars are forbidden.

### 6. Run the gates

```
CATALOG_GATE_MODE=fail uv run pytest server/tests/conformance/test_api_catalog.py -q   # catalog<->manifest
uv run pytest server/tests/conformance/ server/tests/modules/<name>/ -q               # full conformance + module
uv run python scripts/export_connector_registry.py                                     # public registry regen
```

### 7. Ratify live (the only way to lift `verification: blocked`)

The live probe harness `scripts/ratify_connector.py` verifies a connector's
DECLARED contract against a REAL connected account and writes a deterministic,
fully redacted evidence report to
`server/modules/<name>/reports/ratification-<probed_at>.json`. That report is
the ONLY artifact that lifts the public readiness from "Validation required" to
"Verified" (see the chain in step 8). It is **local-only and human-gated
(AI-08)** — NEVER run in CI, and it issues real API requests, so a human runs it
against an account they own.

```
uv run python scripts/ratify_connector.py \
    --module <name> \
    --connection <connection_ref_id> \
    --account <topology-resolved account id> \
    [--tier core|standard|advanced|all]   # default: core \
    [--probe-auth]                          # opt-in: ONE invalid-token request \
    [--probed-at <ISO8601>]                 # default: real clock at run time \
    [--out <path>]                          # default: reports/ratification-<probed_at>.json
```

What it does (per AC1):

- **Field probe.** For every catalog field of the selected tier(s) with
  `exposure` in (exposed, planned), it issues a minimal real request (a 1-day
  window on the selected account) and records `ok | empty |
  rejected(<error_class>) | unsupported`. Requests are built generically from the
  `probe` block in `catalog_sources/catalog_sources.json` — `request_style` (one
  of `fields_param | metrics_dimensions | dimensions_only | report_statistics |
  none`) + `batch_size`. Fields are batched to respect quotas; a rejected/unknown
  batch is **bisected** so the exact offending field is captured (never the whole
  batch). A `RateLimitError` (429) pauses and resumes the SAME batch
  (breaker-aware) so no progress is lost; a batch rate-limited past the retry cap
  is left `unprobed` (surfaced honestly in `coverage.unprobed`).
- **Error probe.** One intentionally invalid FIELD request must classify
  `invalid_request`. Only with `--probe-auth`, one invalid-TOKEN request must
  classify per the `error_map` (`auth_expired`/`auth_revoked`). Nothing is ever
  revoked; no destructive scenarios.
- **Topology probe.** When the module declares `account_topology`, discovery runs
  live and the report asserts the selected account is in the reachable set.
- **`request_style: none`** (generic, google-sheets, github): the catalog IS the
  contract by construction (no per-field API request exists). The harness records
  a **structural ratification** — every exposed/planned field → `ok` with an
  explicit `structural_note` — instead of probing.

The report is deterministic (sorted fields), redacted (only a `conn_***<tail>`
connection marker and the account id — no tokens, AD-3), and carries a `verdict`:
`ratified` (every probed field ok/empty, error contract holds, account reachable,
nothing unprobed) · `partial` (progress made, coverage incomplete — e.g. a
tier-core-only run) · `failed` (any rejected/unsupported field, a broken error
contract, or an unreachable account).

**No-test-account reality (Jean, 2026-07-21).** We have no dedicated test
accounts for most providers, so the pass itself is deferred per connector
(AI-13) and happens the moment a real account exists — the harness is built and
fully tested with mocks (`server/tests/tooling/test_ratify_connector.py`), but a
live run is a manual, human-gated act:

- **First realistic targets: `google-analytics` and `gsc`.** They authenticate
  through the direct Google OAuth path (`get_fresh_token(..., provider=...)`
  routing on the connection's `auth_path`), so Jean can ratify them against his
  OWN GA4/GSC accounts — one consent, all scopes — with no client involved.
- **Paid-media providers (`meta-ads`, `tiktok-ads`, `linkedin-ads`) ratify when
  a client account connects.** Until then they stay `verification: blocked`.
- **Partial tier-core ratification is acceptable and visible.** Running
  `--tier core` first (the cost/impressions/clicks/conversions families + the
  structural ids) yields a `partial` verdict that still records exactly which
  families were verified against which account on which date. The report's
  `coverage` + per-field map make the boundary of what was proven explicit — that
  precision (verified against WHAT, WHEN, at what %) is the whole point.

### 8. Wire the verified state into the public registry (fail-closed)

Once a `reports/ratification-*.json` with `verdict: ratified` and a `probed_at`
date exists, flip the manifest's `public_catalog.verification.status` to
`ratified`. `scripts/export_connector_registry.py` then accepts it (it REFUSES a
`ratified` status that has no valid report — `RegistryValidationError`) and emits
`readiness.status: "verified"` + `verifiedAt` for that connector. The web chain
(`web/src/lib/connectors.ts` → `connector-detail.ts`) renders a "Verified" label
citing the evidence date. "Validation required" remains the default for every
connector that has not been ratified. No manifest is flipped until a real live
run has actually happened.

## Definition of done (per connector)

- [ ] `api_catalog.json` generated (not hand-authored), official source pinned, drift empty
- [ ] Every field tiered; catalog counts match the official reference
- [ ] `error_map` filled from the provider error reference; 401-path module test
- [ ] `account_topology` declared; discovery implemented; no account env vars
- [ ] `CATALOG_GATE_MODE=fail` green for the module; conformance + module tests green
- [ ] Registry regenerated; totals in web assertions updated if profile counts changed
- [ ] Live ratification report committed (or explicitly deferred with reason)

## Maintenance contract (keeping it true over time)

- A provider API version bump ⇒ re-fetch sources, re-run the generator, review
  the diff of `api_catalog.json` (git shows exactly what the provider moved).
- Supermetrics publishes dated "field changes" pages per connector — a cheap
  external signal that a re-generation is due.
- An `invalid_request` on a cataloged field in production logs
  `pull_invalid_request_drift` — that IS the drift alarm; treat it as a bug in
  the catalog contract, not as a flaky pull.

<!-- BEGIN 25.8 catalog-driven execution (delimited to avoid colliding with the 25.6 §7 rewrite) -->

### Catalog-driven execution (25.8)

The catalog is not documentation — it is the **execution contract**. A report
profile can opt into `selection_mode: "catalog_driven"` (alongside the existing
`exact_bundle`) so that *any* cataloged field a datastream selects is actually
extracted. The mode is opt-in per profile; every `exact_bundle` profile stays
bit-identical (no `selection=` is ever passed to it).

**Core contract (AD-2, generic — no provider vocabulary in core):**

1. A pull request carries a field selection `{metrics: [...], dimensions: [...]}`.
2. `core.catalog_contract.validate_selection(catalog, selection)` checks every id
   against the module's `api_catalog.json`. An unknown id is a **typed refusal**
   listing the offending ids (`unknown_selection_field`) — the drift signal,
   never a silent drop. A `None` selection falls back to
   `catalog_default_selection(catalog)` (every non-excluded **tier-core** field,
   partitioned by kind — the honest default, never a hardcoded list).
3. The dispatch path (`core.loader.dispatch_pull` and the queue worker) resolves
   the active capability report; when its `selection_mode == "catalog_driven"` it
   loads the module catalog, validates the selection, and passes
   `selection={"metrics", "dimensions", "source_fields": {field_id: source_field}}`
   to the pull callable. A drifted selection raises
   `core.pull_errors.InvalidRequestError` → the `invalid_request` branch
   (`pull_invalid_request_drift`), exactly like a provider 400.

**Module contract (the reference is `meta-ads` / `pull_catalog_daily`):**

- `pull_<profile>(..., selection=None)` — accept the `selection` kwarg. The
  module owns the provider-specific translation of the generic selection into
  the concrete request: which ids go in the provider's field list vs its
  breakdown/segment param, how expanded ids (e.g. action-family metrics) map
  back to a base array + filter at parse time, and how a wide selection is
  **chunked** and the chunk rows **merged on the grain key**.
- Breakdown/segment compatibility is validated **before** the API call from a
  declaration in `catalog_sources.json` (meta-ads: `breakdown_compatibility` —
  `breakdown_fields`, `incompatible_pairs`, `max_breakdowns`); an incompatible
  combination raises `InvalidRequestError` (never a wasted provider round-trip).
- Landing shape is unchanged: rows land through the module's existing
  `_insert_raw_rows`; dbt marts UNPIVOT to the long fact downstream.

**Exposure truth:** once a field is reachable through catalog_driven execution
its catalog `exposure` becomes `exposed`; fields needing an unsupported request
shape stay `excluded` **with an `exclusion_reason`** (meta-ads records the
excluded families and the `exposed_when` predicate in
`catalog_sources.json → exposure_regeneration`). Regenerate the catalog with the
generator (see the orchestrator command block in the story) — never hand-edit
exposure. Conformance stays green in `CATALOG_GATE_MODE=fail`.

<!-- END 25.8 catalog-driven execution -->

## Money metrics — declare the native encoding (Story 39.2, E39-AD1)

When a connector lands a **monetary** metric (39.1's `monetary:true` set —
revenue/cost/fee/refund/…), it MUST declare how the source encodes that money so the
platform **money adapter** can normalize it to the ONE canonical internal unit —
**micros, currency attached** — divided once at read (the AD-4 ratios-at-view rule's
money sibling):

1. **Declare `native_unit` on the field.** Add a `money` sub-object to the metric's
   `source_capabilities` field (schema: `source-capabilities.schema.json` `$defs.field`):
   `{"money": {"native_unit": "micros" | "decimal" | "cents", "currency_source": "<opaque locus>"}}`.
   This is the adapter INPUT (diffable vs the API doc). `micros` = source already returns
   micros (adapter no-op); `decimal` = ordinary decimal amount (adapter `×1e6`); `cents` =
   minor units (adapter `×10_000`).
2. **Add the canonical row to the seed.** Add the canonical metric to
   `dbt/seeds/money_metric_units.csv` (`canonical_metric,canonical_native_unit,notes`) —
   the provider-agnostic map the read layer keys on. Only money metrics are listed.
3. **Do not divide upstream.** Canonical micros stays integer-exact through staging AND the
   mart; the `/1e6` happens ONCE at read (`core.money.read_units` + the semantic ratio
   views). Ratios (eCPM/CPC/CPA/ROAS) are reconstructed at view time from additive
   components, each monetary component `/1e6` iff canonical micros — sum-then-divide, never
   divide-then-sum, never a per-row `/1e6`.

Zero-drift is a review requirement: adding money to a connector must leave every existing
single-currency total **byte-identical** (proven by a real `dbt build`). FX (currency
conversion) is a separate concern — the adapter normalizes UNIT only and preserves the
source currency as a tag; it never converts.

## Report-timezone capture — declare the time context (Story 39.7, E39-NFR02)

Every **daily** connector captures the exact **report timezone** the source used to draw its
reporting-day boundaries (GAM network `timeZone`, GA4 property timezone, an ad-account
timezone…) as **per-row provenance**, so a downstream consumer (Story 39.8 signalling,
reconciliation) can see which day boundary a figure was drawn on. This generalizes GAM's
existing `report_timezone` capture into a source-agnostic CONTRACT — the time-context sibling
of the money adapter above:

1. **Declare `time_context` on the descriptor.** Add a `time_context` sub-object to the
   connector's `source_capabilities` **descriptor** (schema:
   `source-capabilities.schema.json` `$defs.time_context`; note it sits on the DESCRIPTOR, not
   a field — one report timezone per datastream/network/property, contrast the per-field
   `money`): `{"time_context": {"locus": "network"|"property"|"account"|"fixed"|"none",
   "fallback": "gap"|"assume", "fixed_zone"?, "assumed_zone"?}}`. `locus` is an ABSTRACT source
   of the zone (never a provider field, AD-2): `network`/`property`/`account` = read from
   provider metadata at pull; `fixed` = the provider always reports in one declared zone;
   `none` = the provider exposes no report timezone.
2. **Land `report_timezone` per row.** A conformant daily connector's raw table carries
   `report_timezone VARCHAR` and its staging passes it through UNCHANGED (GAM already does).
   The value is resolved via the platform contract brain
   (`server/core/report_timezone.py:resolve_capture`), which validates the captured IANA zone
   (stdlib `zoneinfo`).
3. **Undetermined = a recorded assumption or a `TIMEZONE_GAP`, NEVER a silent zone.** If the
   report timezone cannot be determined (metadata unavailable, field absent, `locus='none'`,
   invalid IANA), the contract fails **closed**: either the connector's declared `assume`
   posture records an explicitly-declared `assumed_zone` **tagged `assumed=true`** in
   provenance, or a typed `TIMEZONE_GAP` surfaces at the datamodel layer
   (`_detect_conflicts`, shape-aligned with `CURRENCY_GAP`). There is **no code path that
   silently writes `'UTC'` or the project default** (E39-NFR02).

CAPTURE only (Story 39.7): the captured `report_timezone` is **immutable** source provenance
(E39-AD2) — never rewritten, never used to `convert_timezone()` at day grain (dishonest at
DATE grain; HG-4). Signalling cross-source day-offsets and exposing the source adjustment
lever is Story 39.8, which DEPENDS on this capture. Absent declaration = today's behaviour
(the connector captures no report timezone yet); adding the capture column must leave every
existing total **byte-identical** (E39-NFR06).
