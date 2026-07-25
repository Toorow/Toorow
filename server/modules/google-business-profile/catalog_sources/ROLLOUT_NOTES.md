# Google Business Profile connector — rollout notes

Module: `server/modules/google-business-profile/` · API: Performance API v1 (+ Reviews v4 legacy) · Kind: `kpi` · Auth: `google_direct` (direct Google OAuth, scope `business.manage`, NOT Nango — AD-21)
Research dossier: `_bmad-output/implementation-artifacts/research/google-business-profile-catalog-research.md` · Story: `30-1-connecteur-google-business-profile.md`

## Catalog generation

```bash
# official_fields.json is transcribed from the DailyMetric enum + v4/monthly schemas.
uv run python scripts/build_api_catalog.py --module google-business-profile \
    --sources-dir server/modules/google-business-profile/catalog_sources \
    --report server/modules/google-business-profile/catalog_sources/fusion-report.json
```

Fusion report (2026-07-21): `official_total=25`, `drift_ids=[]`, `exposure {exposed:13, planned:12}`. No enrichment source (GBP is not a Supermetrics source) — the official DailyMetric enum + v4/monthly schemas are the sole authority.

## Central design facts (surface to the client)

- **History EXISTS but is hard-capped at ~18 months** (daily). Data older than 18 months from the request date is unreachable, and there is no backfill beyond it — the connector must sync early and persist for anything longer (YoY). Recent days lag ~3–7 days; **zero-days are omitted** from `datedValues` (the pull leaves them NULL; gap-fill to 0 is a mart concern, AD-9).
- **0-QPM access gate.** A newly-enabled GCP project has **0 QPM** until Google manually approves an access/quota request (no sandbox); approved default is 300 QPM. The **v4 Reviews** host is additionally **allowlist-gated**. These are provisioning gates, not auth-flow changes — but they mean the connector is un-runnable (403) until granted.
- The 11 `DailyMetric` values are all **additive daily counts**. Total impressions = **sum of the 4 `business_impressions_*`** (Google exposes no single combined metric — computed downstream, never stored raw).

## Design deviations / boundaries

- **Dedicated wide mart** `fact_gbp_location_daily` for v1. These metrics are additive and belong in the cross-source `fact_daily_kpi`, but wiring them there (new canonical metrics + `dim_metric.csv` rows) is a **follow-up**, deliberately deferred while the central seeds/mart carry open parallel-session merge conflicts (`google-analytics`, `linkedin-ads`, `shopify` files are mid-conflict — NOT introduced by this module). No central file touched.
- **Report pack deferred** (same reason): `reports/*.json` needs the metrics registered in `dim_metric.csv` for `report_dictionary.is_known_metric`.
- **Reviews (v4) + monthly search keywords are planned, not extracted** in v1. Reviews live on the deprecated, allowlist-gated `mybusiness.googleapis.com/v4` host (migration risk) — isolate behind an adapter when built. `averageRating` is non-additive (an average, never summed); monthly keywords are a separate monthly fact with a `value|threshold` union.
- **error_map keyed on HTTP status** (standard Google error envelope, no numeric subcodes). 429 RESOURCE_EXHAUSTED → `RateLimitError` (breaker).
- **Topology account→location**; `discover_accounts` walks `accounts.list` → `accounts.locations.list`; the reporting entity is `locations/{id}`. No `*_LOCATION_ID` env var.

## Verification

`public_catalog.verification.status = "blocked"` — **doubly**: no GBP test account (2026-07-21) AND the default 0-QPM access gate. Ratify once a real GBP account with granted quota exists: probe `fetchMultiDailyMetricsTimeSeries` for one location over a 1-day window (all 11 metrics), then the v4 reviews + monthly keywords once allowlisted.
