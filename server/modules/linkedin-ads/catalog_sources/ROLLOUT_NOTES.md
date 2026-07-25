# LinkedIn Ads — catalog rollout notes (Story 25.7)

Bringing the `linkedin-ads` module to the connector standard (playbook:
`server/modules/README.md`; doctrine:
`_bmad-output/planning-artifacts/epic-25-industrialisation-connecteurs.md`).

## Counts

Curated official snapshot (`official_fields.json`, produced deterministically by
`build_official_fields.py`):

- **153 fields total** = **125 metrics** + **28 dimensions**.
- Metrics: every field in the LinkedIn Marketing adAnalytics **Metrics Available**
  table (moniker `li-lms-2026-06`) verbatim (117 rows) + the **8** Revenue
  Attribution metrics (`attributedRevenueMetrics` finder) flattened.
- Dimensions: **4** structural/time (`date`→`dateRange`, `pivotValues`,
  `campaign_id`, `campaign_group_id`) + **24** pivot enums from the
  Analytics/Statistics finder `pivot` list, each emitted ONCE as a parameterised
  dimension `pivot_<ENUM>` (source_field `pivot:<ENUM>`).

Fusion report (dry generator run, `--sources-dir /tmp/roll-linkedin-ads`):

```
official_total   = 153
matched          = 0        (enrichment placeholder; real supermetrics.md enriches descriptions only)
official_only    = 153
enrichment_only  = []       (no enrichment-only suspects emitted)
drift_ids        = []       (EMPTY — every manifest field exists in the official reference)
exposure_counts  = {exposed: 8, planned: 145}
tier_counts      = {core: 47, standard: 56, advanced: 50}
```

- **8 exposed** = the manifest `source_capabilities.fields` (cost, impressions,
  clicks, conversions, leads, date, campaign_id, campaign_group_id). The rest is
  `planned` (landing the full catalog does not require extraction support;
  extraction widens tier by tier in later stories).
- **drift is EMPTY**: the four canonical manifest field_ids whose provider token
  differs (`cost`←costInLocalCurrency, `conversions`←externalWebsiteConversions,
  `leads`←leadGenerationMailContactInfoShares, `date`←dateRange) are emitted in
  the official snapshot under their canonical `field_id` with the provider token
  as `source_field`, so the manifest↔catalog gate reports 0 issues
  (`source_field_mismatch` and `manifest_field_not_in_catalog` both clean).

## Pinned versions / sources

- `api_version`: **202506** (LinkedIn versioned API `LinkedIn-Version: 202506`,
  in force July 2026; matches the module's existing `_LINKEDIN_API_VERSION`).
- `generated_at`: `2026-07-21T00:00:00Z` (from config, never the clock).
- Official authority: Microsoft Learn adAnalytics reporting + reporting-schema
  (moniker `li-lms-2026-06`); account discovery from the adAccounts
  create-and-manage reference; error handling from the shared error-handling ref.

## enrichment_only justification

Supermetrics (`https://docs.supermetrics.com/docs/linkedin-ads-fields.md`,
101 metrics / 110 dimensions) is declared as **enrichment only** — it is NEVER
the authority for field existence:

- Supermetrics enumerates every **demographic pivot VALUE** permutation
  (per-country, per-industry, per-seniority, per-job-title, …) as a distinct
  dimension column, inflating its 110-dimension count. This catalog declares each
  pivot ONCE as a parameterised dimension; pivot values are query OUTPUTS resolved
  per account at run time (URN resolution), not catalog fields, and would be
  non-deterministic against an account's data.
- Supermetrics also carries connector-tool concepts (utm_*, `dataSourceName`,
  `system_metadata`, AD FORM/AD FORM RESPONSES families, DEPRECATED reach/freq
  metrics) that are Supermetrics-product surfaces, not adAnalytics API fields.
- Therefore any `enrichment_only_id` is reported as a **suspect** only and is
  never emitted into `api_catalog.json`. On a real supermetrics.md, enrichment
  contributes description/section refinement for matched ids; nothing more.

## error_map caveat (AC3)

LinkedIn's error body is `{"message", "serviceErrorCode", "status"}`. Core's
generic `_extract_provider_code` only recognises `error.code` / `error_code` /
top-level `code`, so **serviceErrorCode is NOT extractable** and cannot refine the
pure-HTTP class. LinkedIn also publishes **no distinct numeric subcode** for
expired-vs-revoked (both are HTTP 401, distinguished only by the human-readable
message). The `error_map` is keyed `<status>:<status>` and documented via
`_error_map_note`; the pure-HTTP base class already yields the same class
(401→auth_expired, 403→permission_denied, 400/426→invalid_request,
5xx→provider_transient). Wired via `_load_error_map` at both `classify_http_error`
sites (`_pull` + `discover_accounts`). A mocked 401 → `auth_expired` (test green).

## account_topology (AC4)

Single flat level `ad_account` (`selection_level: ad_account`). Discovery callable
`discover_accounts` calls `GET /rest/adAccounts?q=search` (cursor-paged via
`pageToken` / `metadata.nextPageToken`) through the module's existing Nango token
path and returns `[{"id": "urn:li:sponsoredAccount:<id>", "label": <name>}]`.
Typed errors + `RateLimitError("linkedin-ads")` on 429. The `LINKEDIN_ADS_ACCOUNT_ID`
env-var fallback in `_pull` is now superseded by topology-driven selection.

## Orchestrator command block

The dev agent cannot run shell. The orchestrator generates + gates:

```bash
# 1. stage the curated sources (official + catalog_sources) into a sources dir
mkdir -p /tmp/roll-linkedin-ads
cp server/modules/linkedin-ads/catalog_sources/official_fields.json  /tmp/roll-linkedin-ads/
cp server/modules/linkedin-ads/catalog_sources/catalog_sources.json  /tmp/roll-linkedin-ads/
# fetch the enrichment snapshot (NOT committed):
curl -sL https://docs.supermetrics.com/docs/linkedin-ads-fields.md \
     -o /tmp/roll-linkedin-ads/supermetrics.md

# 2. generate the catalog + fusion report (local-only, deterministic)
uv run python scripts/build_api_catalog.py \
    --module linkedin-ads \
    --sources-dir /tmp/roll-linkedin-ads \
    --report server/modules/linkedin-ads/catalog_sources/fusion-report.json
# writes server/modules/linkedin-ads/api_catalog.json  (drift_ids must be [])

# 3. run the gates
CATALOG_GATE_MODE=fail uv run pytest server/tests/conformance/test_api_catalog.py -q
uv run pytest server/tests/conformance/ server/tests/modules/linkedin_ads/ -q
uv run python scripts/export_connector_registry.py
```

## Definition of done status

- [x] `official_fields.json` generated (not hand-authored), official source pinned, drift empty
- [x] Every field tiered (section_tier_map + overrides); counts match the official reference
- [x] `error_map` filled + `_error_map_note` caveat; 401-path module test green
- [x] `account_topology` declared; `discover_accounts` implemented; no new account env vars
- [ ] `api_catalog.json` + `fusion-report.json` generated by the orchestrator (shell step)
- [ ] Registry regenerated (orchestrator)
- [ ] Live ratification report committed (deferred — Phase B human gate AI-08)

## Review corrections (2026-07-21, fresh-context curation review)

- The fusion block above describes the dev agent's placeholder dry-run (no real
  supermetrics.md present, hence matched=0). The COMMITTED
  `fusion-report.json` — generated by the orchestrator with the real
  Supermetrics snapshot — is authoritative: matched=61, enrichment_only=150
  (Supermetrics-computed rates/permutations, reported never emitted).
  Tier counts (47 core / 56 standard / 50 advanced) were correct.
