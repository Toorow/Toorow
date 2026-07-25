# google-analytics — API catalog rollout notes (Story 25.7)

Brings the `google-analytics` module to the connector standard
(`server/modules/README.md`). Surface for this story is limited to
`server/modules/google-analytics/**` and
`server/tests/modules/google_analytics/**`.

## Counts

Curated official snapshot (`official_fields.json`), from the official GA4 Data
API **API Dimensions & Metrics** schema page
(`https://developers.google.com/analytics/devguides/reporting/data/v1/api-schema`),
pinned `api_version` **GA4 Data API v1**:

| kind | count |
|---|---|
| dimensions | 280 |
| metrics | 78 |
| **total** | **358** |

- Every one of the 15 fields the manifest's `source_capabilities.fields`
  already exposes is present in the snapshot with a matching `kind` and, where
  the platform `field_id` differs from the GA4 `apiName`, an explicit
  `source_field` (10 such fields: `active_users`→`activeUsers`,
  `device_category`→`deviceCategory`, `landing_page`→`landingPage`,
  `page`→`pagePath`, `screen_page_views`→`screenPageViews`,
  `session_campaign`→`sessionCampaignName`,
  `session_source_medium`→`sessionSourceMedium`,
  `first_user_source_medium`→`firstUserSourceMedium`,
  `purchase_revenue`→`purchaseRevenue`, `transaction_id`→`transactionId`).
  `sessions` / `conversions` / `date` / `country` / `newVsReturning` already
  equal their `apiName`, so no `source_field` is emitted. ⇒ the fusion report's
  `drift_ids` must be **empty** and the manifest↔catalog gate must report no
  `source_field_mismatch` / `kind_mismatch`.

- The snapshot is produced by the committed, deterministic
  `build_official_fields.py` (no network, no clock); it is the auditable source
  for the curated list.

## Tiering

`section_tier_map` in `catalog_sources.json`:

- **core** — the day-one web-analytics fields: `SESSION`, `USER`, `EVENT`
  (sessions/users/key-events), `CONTENT`, `SESSION SOURCE`, `USER SOURCE`,
  `GEO`, `DEVICE`, `TIME` (date + source/medium + campaign structure).
- **standard** — `ECOMMERCE`, `ADS` (Google Ads cost/clicks imported into
  GA4), `MANUAL SOURCE` (utm_*), `SEARCH`, `LINKS`, `AUDIENCE`.
- **advanced** — the long tail: `DAILY COHORT`, `CM360`, `DV360`, `SA360`,
  `PUBLISHER` (AdMob), `GAMING`.

Per-field `field_tier_overrides` pin the concrete KPI carriers
(sessions / active_users / conversions / key_events / screen_page_views /
totalRevenue / purchase_revenue / transactions / and the core structural
dimensions) to `core` regardless of their section. `default_tier` = `standard`.

## Enrichment-only justification

Enrichment source: Supermetrics GA4 catalog
(`https://docs.supermetrics.com/docs/google-analytics-4-fields.md`,
**89 metrics / 261 dimensions**). Supermetrics is enrichment, **never** the
authority. A large share of its GA4 fields are Supermetrics-**computed** or
Supermetrics-**shaped** (derived ratios, renamed families, and per-VALUE
permutations of dimensions such as `newVsReturning` buckets or channel
groupings) that are not standard GA4 Data API `apiName`s. Those land in the
fusion report as `enrichment_only` suspects and are **expected** — they are
reported, checked against the official page, and **not emitted** into the
catalog. Custom / property-scoped dynamic fields (`customEvent:*`,
`customUser:*`, `customItem:*`, `dimensionN`, `metricN`, `keyEvents:<name>`)
are likewise excluded from the snapshot: they are per-property and cannot be
cataloged statically without a live connected property.

## error_map

**None declared** — justified in `manifest.json` via `_error_map_note`.
`core.pull_errors._extract_provider_code` reads `error.code`, and the GA4 Data
API (like all Google APIs) sets `error.code` to the numeric HTTP status. So any
refinement key would be redundant with the pure-HTTP classification
(`401`→auth_expired, `403`→permission_denied, `400`→invalid_request,
`5xx`→provider_transient; `429`→`RateLimitError` before `classify_http_error`)
or unreachable (the discriminating `error.status` string / `errors[].reason`
tokens are not what the generic extractor keys on, and core must never hardcode
a provider vocabulary — AD-2 / HG-1). The connector's existing non-429 raise
sites (`classify_http_error(status, body)`, no map) are therefore **correct and
left unchanged**. Proof: `server/tests/modules/google_analytics/test_pull_ga4_errors.py`.

## account_topology

Already declared at Story 25.5 (account → property, selection_level=property,
`discover_accounts`). **Not touched** by this story.

## Orchestrator command block (local only, no network in CI)

The orchestrator places this module's `official_fields.json` +
`catalog_sources.json` + the Supermetrics markdown snapshot into the
sources dir, then runs the deterministic generator:

```bash
# 0. Prepare the sources dir
mkdir -p /tmp/roll-google-analytics
cp server/modules/google-analytics/catalog_sources/official_fields.json  /tmp/roll-google-analytics/
cp server/modules/google-analytics/catalog_sources/catalog_sources.json  /tmp/roll-google-analytics/
# Fetch the enrichment snapshot (DO NOT COMMIT it):
curl -sL https://docs.supermetrics.com/docs/google-analytics-4-fields.md \
  -o /tmp/roll-google-analytics/supermetrics.md

# 1. (Optional) regenerate the curated official snapshot from its builder
uv run python server/modules/google-analytics/catalog_sources/build_official_fields.py

# 2. Generate api_catalog.json + fusion report
uv run python scripts/build_api_catalog.py \
  --module google-analytics \
  --sources-dir /tmp/roll-google-analytics \
  --report /tmp/roll-google-analytics/fusion-report.json
#    Expect: drift_ids == []  (non-empty drift is a bug, investigate)
#            enrichment_only  == the Supermetrics-computed suspects (expected)

# 3. Run the gates
CATALOG_GATE_MODE=fail uv run pytest server/tests/conformance/test_api_catalog.py -q
uv run pytest server/tests/conformance/ server/tests/modules/google_analytics/ -q
uv run python scripts/export_connector_registry.py
```

Commit `api_catalog.json`, `catalog_sources.json`, and the curated
`official_fields.json` (+ `build_official_fields.py`). Do **not** commit the raw
`supermetrics.md`.
