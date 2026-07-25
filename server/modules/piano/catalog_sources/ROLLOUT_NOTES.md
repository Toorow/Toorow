# piano (Piano Analytics) -- ROLLOUT NOTES (Story 28.1)

Status: `public_catalog.verification: blocked` (`live_evidence_not_ratified`).
Everything below is PROBE-ONLY: it needs live credentials (a test Piano site
with real custom Data Model keys) and cannot be proven by the mocked test
suite. Evidence goes to `reports/ratification-<date>.json` (25.6 contract) and
the story Dev Agent Record.

## 0. THE dynamic-catalog fact (why this connector is different)

Piano Analytics has NO fixed reporting schema and NO public metadata endpoint.
The committed `api_catalog.json` is the STANDARD BASELINE (45 explicit metrics +
103 standard property keys = 148 fields, research dossier Annexes A+B -- no
round-number padding). `field_discovery.mode =
dynamic` (catalog_sources.json). A columns key unknown to the baseline is NOT
refused in advance by `pull_catalog_daily` (it may be a valid custom key of the
site): it is SENT to getData; an unknown key returns a 400 `InvalidColumns_*`
which the connector surfaces as `InvalidRequestError` + the
`pull_invalid_request_drift` log signal. Completeness is baseline + live
validation, never a fabricated per-site list.

## 1. Data Model admin metadata SPIKE (the true completeness fix)

- [ ] Confirm/deny a Piano management/admin API that lists the Data Model
      (properties + metrics) for a site (the UI reads it from SOMEWHERE). It is
      NOT the v3 Data API and is undocumented in the reachable developer
      portal. If it exists and the same `x-api-key` (access+secret) can read
      it, `discover_accounts` / a future `discover_fields` can generate the
      full per-site catalog programmatically (the true GA4-metadata
      equivalent). Until confirmed, do NOT claim it -- the baseline +
      validate-on-use is the honest contract.
- [ ] Endpoints to probe first: `analytics.piano.io/datamanagement` (UI),
      `api.atinternet.io/v3/...` management variants, any `dataModel` /
      `properties` / `metrics` listing route.

## 2. Site topology (no site-enumeration endpoint)

- [ ] There is NO documented v3 endpoint that lists the sites an API key can
      reach (contrast GA4 `accountSummaries.list`). v1 `discover_accounts`
      VERIFIES candidate site ids (from the `PIANO_CANDIDATE_SITE_IDS`
      discovery input) via a 1-day getData access-check probe and returns only
      the reachable ones. Confirm at the probe that an `UnauthorizedSite`(403)
      / `InvalidSpace_NoActiveSite` on an unreachable site is correctly dropped
      (not selectable) and a reachable site returns 200.
- [ ] Site selection/access-check/trial/backfill are the CORE topology flows;
      `_resolve_site_id` reads the selected site from the topology scope
      (connection_ref) -- there is NO `PIANO_*_SITE_ID` account fallback in the
      pull path (doctrine).

## 3. Auth (single API key = access + secret)

- [ ] Header `x-api-key: <access>_<secret>` built from env `PIANO_ACCESS_KEY` /
      `PIANO_SECRET_KEY`. Confirm the exact concatenation form the live API
      expects (`<access>_<secret>` per the dossier) -- adjust `_api_key()` if
      the provider uses a different separator.
- [ ] The key inherits the CREATING USER's site permissions (over-broad if an
      admin created it). Document in onboarding so clients scope the creating
      user narrowly.
- [ ] Confirm a revoked/disabled key lands `401 BadAuthentication_*` ->
      `auth_expired` with the reconnect affordance (AD-15).

## 4. getData request/response shape (probe with 1-day window, one site)

- [ ] `POST /v3/data/getData` accepts the body
      `{columns, space:{s:[<site_id>]}, period:{p1:[{type:D,start,end}]}, sort,
      max-results, page-num}` and returns `{"DataFeed":[{"Rows":[{...}], ...}]}`.
      `_rows_from_datafeed` handles the DataFeed shape and a bare `Rows` list;
      refuse anything else (drift signal).
- [ ] Confirm row keys are the requested column tokens (property key /
      m_-metric key) -- the `_flatten_row` contract. `date` IS a requestable
      column (a property), unlike GA4/Pinterest where DATE is a response-only
      key.
- [ ] Confirm `getRowCount` shape for paging planning (`_get_row_count` is
      best-effort; the short-page loop is the fallback).
- [ ] Confirm the 200k ceiling + 10k/page paging; verify the `truncated` flag
      fires only when the ceiling is hit with a full last page.
- [ ] `(not set)` sentinel handling: dimension `(not set)` -> `unknown` bucket
      (never dropped).
- [ ] **max_columns chunk-merge (F-1):** send `pull_catalog_daily` a selection
      of > 50 columns (few dimensions, many metrics) and confirm `_chunked_getdata`
      splits it into >=2 getData calls SHARING the dimension columns + the same
      first-dimension-ASC sort, and that the per-chunk rows merge on the (period,
      dimension-token tuple) grain to the UNION of the metric columns (no cell
      dropped, `requests_made` == number of chunks). Confirm the None-default is
      bounded to <= 50 columns (single call, `piano_default_selection_bounded`).
      Confirm a > 50-column selection with too many DIMENSIONS to chunk raises
      the typed "selection exceeds max_columns ... reduce or split" refusal
      (provider_status None) -- DISTINCT from the InvalidColumns 400 drift.

## 5. Dynamic-catalog drift (THE central live probe)

- [ ] Send `pull_catalog_daily` a selection containing a KNOWN-BAD column
      (`columns:[..., "definitely_not_a_key"]`) on the test site and confirm a
      400 `InvalidColumns_*` -> `InvalidRequestError` + the
      `pull_invalid_request_drift` log line. Confirm the EXACT `InvalidColumns_*`
      subcode against the live error-codes table and that `_code_prefix`
      reduces it to `InvalidColumns` so the error_map key matches.
- [ ] Send a selection with a REAL custom key of the test site (a `custom_*` or
      client-named property) and confirm it is ACCEPTED and lands (proving the
      dynamic surface -- an unknown-to-baseline key is not a false refusal).

## 6. Non-additive metrics (AD-4)

- [ ] Confirm that `m_visits` / `m_unique_visitors` breakdown sums do NOT equal
      the site total (Piano "Why can't we sum visits on page analyses?"). The
      catalog marks them `non_additive` (catalog_sources `non_additive_metrics`)
      and the landing carries `non_additive=TRUE`; the mart/semantic layer must
      use `getTotal` for true totals, never a breakdown SUM.

## 7. Concurrency / rate limit (509)

- [ ] Confirm `QuotasExceeded_TooManyRequests` -> HTTP 509 -> `RateLimitError`
      (breaker) and that pulls are serialized per connection (the paging loop is
      sequential; `discover_accounts` probes candidates serially -- NO parallel
      fan-out against one key).
- [ ] Verify `Retry-After` header semantics (seconds) if present.

## 8. Error taxonomy confirmation (ratify_connector --probe-auth)

- [ ] `401 BadAuthentication_*` -> auth_expired.
- [ ] `403 UnauthorizedSite` / `403 InvalidSpace_NoActiveSite` -> permission_denied.
- [ ] `400 InvalidColumns_*` (unknown column) -> invalid_request + drift.
- [ ] `400 InvalidJSON/InvalidSort/InvalidPeriod/InvalidFilter/InvalidSegment`
      -> invalid_request.
- [ ] `509` / `429` -> RateLimitError with honored retry_after.
- [ ] `5xx UnknownError` -> provider_transient.

## Orchestrator commands (BLOCKED for the dev agent -- no shell)

```bash
# Regen (byte-stability check of the committed artifacts -- deterministic):
uv run python server/modules/piano/catalog_sources/build_official_fields.py
uv run python scripts/build_api_catalog.py --module piano \
    --sources-dir server/modules/piano/catalog_sources \
    --report server/modules/piano/catalog_sources/fusion-report.json

# Tests:
uv run pytest server/tests/modules/piano/ -v
uv run pytest server/tests/conformance/ --module-path server/modules/piano/ -v
uv run pytest server/tests/conformance/test_all_module_capabilities.py -q
uv run ruff check server/modules/piano server/tests/modules/piano

# dbt (local gate, not CI):
(cd dbt && dbt parse)
(cd dbt && dbt build --select stg_piano_daily)
```
```
```
