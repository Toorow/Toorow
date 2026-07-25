# DoubleVerify Catalog Sources — Rollout Notes

## Counts

| Category | Count |
|---|---|
| Official fields (official_fields.json) | 116 |
| Official metrics | 72 |
| Official dimensions | 44 |
| Manifest source_capabilities.fields | 16 |
| Fields matching manifest (drift) | 0 |
| Catalog exposed | 88 |
| Catalog excluded (rate/mean sections) | 28 |
| Tier core / standard / advanced | 70 / 38 / 8 |

Drift is **zero**: every `source_capabilities.fields` entry in `manifest.json`
has a matching `official_fields.json` entry with consistent `kind` and
`source_field` (all identity: `source_field == field_id`).

## Scope & honesty

The DoubleVerify **Report Data API** developer reference
(`developer.doubleverify.com/docs/client-integrations/extensions/report-data-api`)
is gated behind an email-verification login. The field vocabulary is therefore
taken verbatim from the **DoubleVerify field catalog published by Supermetrics**
(which mirrors the Data API `dimensions`/`metrics` ids), cross-checked against
the **Adverity**, **Improvado**, **Alli**, and **Salesforce/Datorama**
DoubleVerify connector docs. The exact request/response JSON, base URL, HTTP
methods, HTTP status codes, and poll interval are the only material gaps — they
are marked as such in `connector.py`/`manifest.json` and MUST be confirmed in the
live-integration pass (AI-13).

**Deliberately NOT enumerated** (inventing their ids without a verifiable source
would violate the honesty rule):

- **Media Gardens (social)** report families — the `meta__` / `tiktok__` /
  `snapchat__` / `twitter__` / `youtube__` prefixed variants. Each is a separate
  report type with its own per-channel data-lag; a follow-up story.
- **Full DV Authentic Attention** (Ad Focus, Dwell Time, Attention Index, 50+
  exposure/engagement signals). This is a separate DV product; there is no
  verified evidence it is reachable through the Standard-report CSV Data API. The
  `AUTHENTIC` section here covers only the authentic viewability/quartile counts
  that the Standard report exposes.

## Non-additive rates (AD-4)

28 fields across six sections (`VIEWABILITY_RATE`, `AUTHENTIC_RATE`,
`FRAUD_RATE`, `BRAND_RATE`, `GEO_RATE`, `AVERAGE`) are declared for completeness
but marked `excluded` with an `exclusion_reason`. Every DV `*_rate` is a
non-additive ratio and `average_time_s_display_viewable_impressions` is a
per-impression mean. `transform()` drops them before landing; they are recomputed
at the semantic layer from their numerator/denominator counts (e.g.
`viewable_rate = viewable_impressions / measured_impressions`). They are never
stored raw and never summed.

## error_map: justified HTTP-status-only keying

DV's error reference is behind the developer login, so no enumerated
application-level sub-code set is verifiable. `error_map` is keyed on HTTP status
only; `core.pull_errors.classify_http_error` resolves the actionable classes
(401→auth_expired, 403→permission_denied, 400/404→invalid_request,
5xx→provider_transient). The one confirmed provider-specific error — the message
`"There is a problem with the selected combination of dimensions and/or metrics"`
on an invalid combo — is an HTTP 400 → `invalid_request` and needs no numeric
refinement. Documented as `_error_map_note` in `manifest.json`.

## account_topology: justified absence

There is **no** account/advertiser enumeration endpoint. A DV **Access Token
Hash** is scoped to the **Reporting Programs** selected at token-creation time in
DV Pinnacle; advertiser, campaign, placement, and media_property are report
**dimensions**, not list endpoints. No `discover_accounts` is implemented and no
`*_ACCOUNT_ID` env var is used. Accounts/advertisers are discovered empirically
by selecting `advertiser_name`/`campaign` as dimensions and reading the distinct
values. Documented as `_account_topology_note` in `manifest.json`.

## report pack: deferred (optional)

No `reports/*.json` report pack ships in this first cut. The report layer
(Layer 6) validates each report's `metrics[]` against `dbt/seeds/dim_metric.csv`,
and DoubleVerify's metrics (monitored_ads, viewable_impressions, fraud/SIVT,
brand-suitability, ...) are not yet seeded there. Seeding them is a shared-seed
change and `dim_metric.csv` is under concurrent edit by the parallel
catalog_daily rollout, so it is left to a coordinated follow-up (klaviyo — a
shipped connector — likewise ships without a report pack; AC2.7 makes it
optional). Add `reports/daily_summary.json` once the DV metrics are seeded.

## auth: static Bearer token (api_key)

DoubleVerify uses a static, long-lived Access Token Hash minted in the DV
Pinnacle UI (Analytics → Data API → Create token) — no OAuth, no refresh. Sent as
`Authorization: Bearer <hash>`. Per AD-3 the secret transits ONLY via Nango
(`get_fresh_token(connection_id, provider="doubleverify")`).

## Orchestrator command block

```bash
# Step 1: official snapshot is committed as official_fields.json
#   (curated from the Supermetrics DoubleVerify field catalog; DV's own
#    reference is login-gated so no shell fetch is possible).

# Step 2: generate the catalog (drift must be empty)
uv run python scripts/build_api_catalog.py \
  --module doubleverify \
  --sources-dir server/modules/doubleverify/catalog_sources \
  --report server/modules/doubleverify/catalog_sources/fusion-report.json

# Step 3: verify the catalog gate
CATALOG_GATE_MODE=fail uv run pytest server/tests/conformance/test_api_catalog.py -q

# Step 4: run the full conformance suite for the module
uv run pytest server/tests/conformance/ --module-path server/modules/doubleverify/ -v

# Step 5: regenerate the public connector registry
uv run python scripts/export_connector_registry.py \
  --output web/src/generated/connector-registry.json
```
