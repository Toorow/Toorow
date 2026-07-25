# Google Ad Manager — catalogue rollout notes

## Source of truth
REST-only. GAM SOAP is deprecated/removed; the **Interactive Reports API**
(`admanager.googleapis.com/v1/networks/{networkCode}/reports`) is the sole data path.
Reports are **async jobs** (submit → poll → fetch rows).

The field catalogue is **ported from `gam-native`** (Jean's repo, contains the orbiads product):

| Asset | gam-native path |
|---|---|
| Generated catalogue (765 dims / 527 metrics, rev `20260528`) | `backend/src/domain/gam_reporting_catalogue_generated.py` |
| Generator (regenerable) | `scripts/coverage-audit/7_generate_reporting_catalogue.py` |
| Discovery snapshot | `_audits/coverage-current/gam_rest_discovery_v1.json` |
| REST client (submit_report + poll) | `backend/src/adapters/gam_rest_client.py` |
| Domain semantics (MONEY micros, report types, compat) | `backend/src/domain/gam_reporting.py` |

**Ownership check (before importing code):** confirm this reporting code is not entangled with
the commercial orbiads product IP. We reuse the *pattern + catalogue*; verify the code is reusable.

## Port checklist (this module)
- [ ] Port `official_fields.json` from `AVAILABLE_DIMENSIONS` / `AVAILABLE_METRICS`
      (name + description + **data_format** + report-type compatibility). REST-only (drop SOAP-only).
- [ ] Port `report_type_compatibility` from `REPORT_TYPE_DIMENSIONS` / `REPORT_TYPE_METRICS`.
- [ ] Generate `api_catalog.json`:
      `uv run python scripts/build_api_catalog.py --module google-ad-manager --sources-dir server/modules/google-ad-manager/catalog_sources --out server/modules/google-ad-manager/api_catalog.json --report server/modules/google-ad-manager/catalog_sources/fusion-report.json`
      (`drift_ids` must be empty.)
- [ ] Wire `connector.pull()` → ported `gam_rest_client.submit_report` + poll loop.
- [ ] Wire `discover_accounts()` → `list_networks_with_access_token` (GET networks).
- [ ] Finalise `error_map` (or `_error_map_note`) from `_summarize_gam_error` / `_raise_report_status`.
- [ ] Confirm `quota` against GAM REST rate limits (async-job cost).

## Semantic guard (do NOT collapse)
See `gam-connector-from-gam-native` memory.
- `AUDIENCE_SEGMENT_*` = GAM **ad-server** audiences (targeting) — **not** GA4-built audiences.
- `GOOGLE_ANALYTICS_*` = GA↔GAM **ad-monetization link** view — **not** GA4 behavioral data.
- Both keep **distinct canonical targets + provenance** from any GA4 connector field.

## Money contract (decision 2026-07-22 — handle with care)
- **Micros to the end**: MONEY metrics stay in **micros** through raw → staging → mart
  (integer-exact in DOUBLE). The `÷1e6` happens **once, at read**, together with the
  currency. `transform()` does **not** divide (per-row division accumulates float drift on
  aggregation). MONEY set is derived from the catalogue `` Data format: `MONEY` `` marker —
  keep that marker in ported descriptions.
- **Currency + report timezone captured at pull()** from the network (`currencyCode`,
  `timeZone`) and stored per row (`currency`, `report_timezone` columns). Revenue is never a
  naked number; never sum across currencies ("choux != carottes"). The report timezone
  explains day-boundary offsets when reconciling with GA4/Shopify.
- **NOT in this connector**: FX conversion + cross-datastream money reconciliation = a
  **separate shared money module** (helper: fixed / as-of-day rate via API). Plan separately.
- **Ratios are NOT imported — reconstructed** (AD-4): CTR, eCPM, CPM-rate, CPC-rate, fill
  rate, viewability % are dropped in `transform()` (metric fields flagged `non_additive`) and
  reconstructed in the mart from additive components (ctr = clicks/impressions,
  ecpm = revenue/impressions*1000, cpc = revenue/clicks). **Trap**: `AD_SERVER_CPM_AND_CPC_REVENUE`
  is REVENUE (additive, kept in micros) — NOT a rate, despite its name. The port must flag the
  real rate metrics `non_additive: true` so the drop set is populated.
- **Do NOT collapse revenue metrics**: GAM has many overlapping `*_REVENUE` (CPM_AND_CPC,
  ALL_REVENUE, line-item-level, ad-exchange, ...). Map to DISTINCT canonical targets; the
  project picks the reconciling one — never sum overlapping revenue metrics.

## Calculated metrics — declared-but-reconstructed (contract 2026-07-22)
A provider-computed metric (CTR, eCPM, CPM/CPC rate, fill rate, viewability %) **exists in the
API** and is **declared** in the catalogue (honest completeness — a datastream can see it is
available), but its value is **not trusted as an additive fact**. The `source_capabilities`
field descriptor now supports an optional **`derivation`** object (see
`server/core/schemas/source-capabilities.schema.json`) that carries the reconstruction so the
semantic/mart layer generates it instead of hardcoding per connector:
```json
"derivation": {
  "method": "ratio",              // or "expression"
  "numerator": "clicks",          // canonical additive component
  "denominator": "impressions",
  "scale": 1,                     // 1000 for eCPM/CPM, 100 for a percentage
  "expression": null,             // e.g. "clicks / impressions" when method="expression"
  "imported": false               // false = drop provider value + recompute; true = also land raw non-additive for parity, never summed
}
```
Port task: for each ratio metric exposed in a report profile, set `non_additive: true` **and** a
`derivation` (ctr = clicks/impressions; ecpm = revenue/impressions ×1000; cpc = revenue/clicks;
fill_rate = impressions/ad_requests; viewability = viewable/measurable). Non-exposed ratios stay
in `api_catalog.json` as `planned` (declared, reconstructable) without a manifest `derivation`.

## Pivot profile
`HISTORICAL` carries in one report type: inventory + revenue + **audiences** (`AUDIENCE_SEGMENT_*`
+ `AUDIENCE_SEGMENT_COST`) + **Nielsen** (`NIELSEN_*`) + **GA-link** (`ANALYTICS_PROPERTY_*`,
`GOOGLE_ANALYTICS_*`). It is the pivot for the daily connector.
