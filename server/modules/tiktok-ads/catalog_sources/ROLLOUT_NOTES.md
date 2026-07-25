# TikTok Ads — catalog rollout notes (Story 25.7)

Brings the `tiktok-ads` module to the connector standard (playbook:
`server/modules/README.md`; doctrine:
`_bmad-output/planning-artifacts/epic-25-industrialisation-connecteurs.md`).

## Counts

Curated official snapshot (`official_fields.json`, produced deterministically by
`build_official_fields.py`):

| | count |
|---|---|
| total fields | 347 |
| metrics | 313 |
| dimensions | 34 |

Enrichment reference (Supermetrics, published totals): **387 metrics / 153
dimensions**. Our metric total (313) lands on the same order of magnitude. The
dimension total (34) is intentionally lower — see the enrichment justification
below.

All **10** manifest `source_capabilities.fields` are present in the snapshot with
matching `kind` AND `source_field` (verified by
`server/tests/modules/tiktok_ads/test_catalog_tiktok.py`):
`spend, impressions, clicks, conversions, date (→ stat_time_day),
campaign_id, campaign_name, adgroup_id, adgroup_name, ad_id`.

## Sources

- **Official (authority):** TikTok Business (Marketing) API **v1.3**
  - Basic report supported metrics: `https://business-api.tiktok.com/portal/docs/basic-reports-supported-metrics/v1.3`
  - Basic report supported dimensions: `https://business-api.tiktok.com/portal/docs/basic-reports-supported-dimensions/v1.3`
  - Basic reports endpoint (`/report/integrated/get/`): `https://business-api.tiktok.com/portal/docs/basic-reports/v1.3`
  - Error codes: `https://business-api.tiktok.com/portal/docs?id=1737172488964097`
- **Enrichment (never authority):** `https://docs.supermetrics.com/docs/tiktok-ads-fields.md`
  (file `supermetrics.md`, **not committed** — enrichment snapshot only).

### Portal-render caveat (AI-53)

The `business-api.tiktok.com` portal is a client-side SPA: its metric/dimension
tables are **not** retrievable by a plain HTTP fetch (the fetch returns only the
page shell). The curated lists therefore reflect the v1.3 BASIC references above
cross-checked against the Supermetrics enrichment section structure
(`ACCOUNT | AD | AD GROUP | ATTRIBUTION | AUDIENCE | BASIC | BUSINESS CENTER |
CAMPAIGN | CONVERSION | ENGAGEMENT | IN APP EVENT | IN APP EVENT (SKAN) |
ONSITE EVENT | PAGE EVENT | REACH | VIDEO`). Every field is a **declared**
contract; live ratification (playbook step 7) confirms exact ids and per-tier
availability. The builder is deterministic and byte-stable (no network, no clock).

## enrichment_only justification (dimension divergence)

Our 34 dimensions vs Supermetrics' 153 is **by design**, not undercoverage.
Supermetrics counts every breakdown **value permutation** (each country, each
age bucket, each gender, each placement) as a distinct dimension column. This
catalog declares each breakdown **once** as a parameterised dimension
(`country_code`, `age`, `gender`, `placement`, …). Enumerating breakdown values
is out of scope — they are query parameters, not catalog fields, and would be
non-deterministic against a specific account's data. The same discipline is
applied by the `meta-ads` reference module.

Conversely, the metric side is **expanded** per event type: the `conversion`,
`cost_per_conversion`, `conversion_rate`, `real_time_conversion`, in-app
`total_*` / `cost_*`, SKAN `skan_total_*` / `skan_cost_*`, `onsite_*` and
`page_event_*` families are flattened over the official event-type lists
(`complete_payment`, `app_install`, `purchase`, `on_web_order`, …), mirroring
how the reporting API surfaces per-optimization-event metrics and how
Supermetrics flattens the same sections.

## error_map (AC3)

`manifest.json → error_map` refines the **HTTP raise site only** (the non-200,
non-429 path in `_pull` and `discover_accounts`). Keys are
`"<http_status>:<provider_code>"` where `provider_code` is TikTok's top-level
logical `code` echoed in the error body (`core.pull_errors._extract_provider_code`
reads the `{"code": X}` shape directly). Canonical codes (verified 2026-07-21):

| status:code | canonical class | meaning |
|---|---|---|
| `401:40105` | `auth_expired` | access token incorrect / revoked |
| `401:40002` | `auth_expired` | access token invalid / expired |
| `401:40100` | `auth_expired` | authentication failed |
| `401:40000` | `auth_expired` | invalid access token |
| `401:40001` | `auth_revoked` | app not authorized for advertiser |
| `403:40001` | `permission_denied` | app not authorized (permission surface) |
| `403:40003` | `permission_denied` | insufficient permission / invalid app id |
| `403:40300` | `permission_denied` | no permission |
| `400:40002` | `invalid_request` | invalid parameter |
| `400:40001` | `invalid_request` | invalid parameter (app not authorized surface) |
| `500:50000`, `500:50002` | `provider_transient` | server error |

429 keeps raising `core.quota.RateLimitError("tiktok-ads")` (breaker path,
unchanged).

### Deferred: the logical `code != 0` path (HTTP 200 business errors)

TikTok returns **HTTP 200 with a logical `code` field** for business errors as
well as real HTTP errors. This story wires the taxonomy **only** for the real
HTTP raise site (per the playbook — do not restructure the logical path now).

The logical path in `_pull` still raises a bare `RuntimeError` today:

```python
if payload.get("code") not in (0, None):
    raise RuntimeError(f"TikTok API logical error code={api_code}: ...")
```

**Planned adoption (later story):** route this branch through the same
`error_map` so a logical `40105` on a 200 body also yields a typed
`AuthExpiredError` with the payload preserved. The clean approach is a small
helper `classify_logical_error(code, payload, error_map)` that maps a bare
provider code (no HTTP status) via a parallel `"logical:<code>"` key space, or
reuses the HTTP map by synthesising the equivalent status (e.g. treat a logical
`40105` as `401:40105`). Until then the logical path stays `RuntimeError` — the
existing tests (`test_pull_logical_error_code_raises_runtime_error`,
`test_pull_missing_code_raises_runtime_error`) pin that contract, so the change
is a deliberate, tested migration rather than a silent behaviour drift.

## account_topology (AC4)

Single flat level `advertiser` (`selection_level: "advertiser"`). Discovery
callable `discover_accounts` calls the official
`GET /open_api/v1.3/oauth2/advertiser/get/` (app_id + secret + `Access-Token`
header) and returns `[{"id": advertiser_id, "label": advertiser_name}]`. Typed
errors via the error_map; `RateLimitError("tiktok-ads")` on 429.

`app_id` / `secret` are the OAuth **app** credentials
(`TIKTOK_APP_ID` / `TIKTOK_APP_SECRET`, module-owned config) — **not** an account
id env var. `TIKTOK_ADS_ADVERTISER_ID` is now a **deprecated** fallback: the
advertiser id should come from the core selection flow (playbook §5), not env.
Remove the env fallback once core selection is wired end-to-end for this module.

## Orchestrator command block (local-only generator + gates)

```bash
# 1. stage sources (the curated official snapshot is committed; add enrichment)
mkdir -p /tmp/roll-tiktok-ads
cp server/modules/tiktok-ads/catalog_sources/official_fields.json /tmp/roll-tiktok-ads/
cp server/modules/tiktok-ads/catalog_sources/catalog_sources.json /tmp/roll-tiktok-ads/
curl -sL https://docs.supermetrics.com/docs/tiktok-ads-fields.md \
  -o /tmp/roll-tiktok-ads/supermetrics.md   # enrichment snapshot; DO NOT COMMIT

# 2. (re)generate the deterministic official snapshot if the builder changed
uv run python server/modules/tiktok-ads/catalog_sources/build_official_fields.py

# 3. fuse official + enrichment -> api_catalog.json + fusion-report.json
uv run python scripts/build_api_catalog.py \
  --module tiktok-ads \
  --sources-dir /tmp/roll-tiktok-ads \
  --report server/modules/tiktok-ads/catalog_sources/fusion-report.json

# 4. run the gates
CATALOG_GATE_MODE=fail uv run pytest server/tests/conformance/test_api_catalog.py -q
uv run pytest server/tests/conformance/ server/tests/modules/tiktok_ads/ -q
uv run python scripts/export_connector_registry.py
```

Review the fusion report: `drift_ids` MUST be empty (every manifest field is in
the official reference). `enrichment_only_ids` are suspects to check against the
official doc, never emitted.

## Live ratification (deferred)

`public_catalog.verification.status` stays `blocked` until the live-probe harness
(WSL, real advertiser, human-gated AI-08) runs against every declared field and
every mapped error code and commits
`server/modules/tiktok-ads/reports/ratification-<date>.json`.
