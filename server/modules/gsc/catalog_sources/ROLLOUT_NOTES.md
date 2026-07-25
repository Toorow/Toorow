# GSC Catalog Sources — Rollout Notes (Story 25.7)

## Counts

| Category | Count |
|---|---|
| Official fields (official_fields.json) | 12 |
| Official metrics | 4 (clicks, impressions, ctr, average_position/position) |
| Official dimensions | 8 (query, page, country, device, date, hour, search_type, searchAppearance) |
| Manifest source_capabilities.fields (pre-existing) | 11 |
| Fields in official snapshot matching manifest (drift=0) | 11 |
| Fields exposed (in manifest) | 11 |
| Fields planned (official but not yet extracted) | 1 (ctr — excluded by AD-4 ratio rule; counted in official, excluded from raw storage) |
| Supermetrics total metrics | 10 |
| Supermetrics total dimensions | 73 |
| Enrichment-only fields (Supermetrics-computed, not in raw API) | ~65 dimensions + 6 metrics |

## Drift notes

All 11 existing `source_capabilities.fields` entries in `manifest.json` have a matching entry in `official_fields.json` with consistent `kind` and `source_field`. Drift is **zero**.

- `search_type` maps to the API `type` request parameter — it is not returned as a `keys[]` dimension but is stamped on every raw row by the connector. It is listed in the official snapshot as a dimension because it is a first-class field in the canonical schema.
- `ctr` (ratio metric) is present in the official snapshot for completeness but is intentionally excluded from raw storage per AD-4 (it is computed at the semantic layer from clicks/impressions). Its exposure in any generated `api_catalog.json` should be `excluded` with `exclusion_reason: "ratio_metric_computed_at_semantic_layer"`.
- `average_position` is the canonical name; the GSC API returns the field as `position` — the `source_field` in `official_fields.json` is set to `"position"` accordingly.

## Enrichment-only justification

The Supermetrics Google Search Console catalog lists 10 metrics and 73 dimensions, far exceeding the 12 official GSC API fields. The delta consists entirely of Supermetrics-computed fields:

- **Landing page analysis** (11 dims): title tag, H1, meta description, canonical, meta robots, meta viewport, GTM ID, GA UA ID, keyword-in-HTML checks — all extracted by Supermetrics by crawling the landing page, not available from the GSC searchanalytics API.
- **Branded/non-branded segmentation** (3 dims): user-defined brand term classification applied at query time by Supermetrics — no GSC API equivalent.
- **URL parsing** (9 dims): protocol, hostname, path segments, query parameters, anchor — extracted by Supermetrics from the landing page URL string.
- **SERP position groupings** (2 dims): rounded avg. position, paging groups — computed from `position`.
- **Supermetrics system metadata** (12+ dims): query timing, timezone, data source account info — connector-layer metadata, not GSC content.
- **Extra metrics**: `# of words in search query`, Submitted/Indexed URL counts, Warnings, Errors — the latter four are from the GSC Index Coverage report (a different API endpoint) and are not available via searchanalytics.query.

All Supermetrics-computed fields are `enrichment_only` and must not be emitted in the generated `api_catalog.json` as `exposure: "exposed"` — they are suspects at best and non-existent in the raw searchanalytics response.

## error_map: justified absence

`error_map` in `manifest.json` is an empty object. Google Search Console uses standard HTTP status codes only — error responses follow the Google API Error model `{ "error": { "code": <http_status>, "message": "...", "errors": [...] } }` with no application-level numeric subcode equivalent to Meta's `error.code` / `error.subcode`. The pure-HTTP taxonomy in `core.pull_errors.classify_http_error` already covers all actionable classes without refinement:

- 401 → `auth_expired` (token expired or revoked)
- 403 → `permission_denied` (site not verified or scope missing)
- 400 → `invalid_request` (malformed query, invalid dimension combination)
- 5xx → `provider_transient` (Google-side error, retryable)

No provider-code refinements are available or needed. This is documented as `_error_map_note` in `manifest.json`.

## account_topology

GSC topology is **single-level** (`selection_level: "site"`). The `discover_accounts` function (added in `connector.py`) calls `GET https://www.googleapis.com/webmasters/v3/sites` with a Bearer token (same token-acquisition pattern as `pull()` — `nango_client.get_fresh_token(connection_id, provider="gsc")`). It returns a flat list of `{"id": "<siteUrl>", "label": "<siteUrl>"}` objects. There is no parent account level — each GSC property (URL-prefix or domain property) is directly selectable.

### GSC_SITE_URL deprecation (migration guide)

Prior to story 25.7, GSC pulls were configured via the `GSC_SITE_URL` environment variable, resolved in `_resolve_site_url()`. This env-var pattern is deprecated in favour of the core topology flow (account_topology.resolve_selected_account).

**Migration steps (for operators):**
1. Run the onboarding flow for the existing connection: `discover_accounts` will enumerate all verified sites the OAuth token can reach.
2. Select the site that matches the current `GSC_SITE_URL` value.
3. The core topology will store the selected `siteUrl` and pass it to pull functions at runtime.
4. Once core topology is wired and the selected site is confirmed, remove `GSC_SITE_URL` from the environment.

**No immediate action required.** The `_resolve_site_url` fallback remains in place and existing pull schedules continue to work until core topology is live. The env-var is not removed in this story.

## Orchestrator command block

```bash
# Step 1: fetch official snapshot (already committed as official_fields.json)
# (No shell fetch needed — the GSC API is small and fully enumerable by inspection)

# Step 2: fetch Supermetrics enrichment snapshot (DO NOT COMMIT)
curl -sL https://docs.supermetrics.com/docs/google-search-console-fields.md \
  -o /tmp/roll-gsc/supermetrics.md

# Step 3: copy sources
cp server/modules/gsc/catalog_sources/catalog_sources.json /tmp/roll-gsc/
cp server/modules/gsc/catalog_sources/official_fields.json /tmp/roll-gsc/

# Step 4: run the catalog generator
uv run python scripts/build_api_catalog.py \
  --module gsc \
  --sources-dir /tmp/roll-gsc \
  --report /tmp/roll-gsc/report.json

# Step 5: verify gate (drift must be empty)
CATALOG_GATE_MODE=fail uv run pytest server/tests/conformance/test_api_catalog.py -q -k gsc

# Step 6: run module tests
uv run pytest server/tests/modules/gsc/ -q

# Step 7: regenerate registry
uv run python scripts/export_connector_registry.py
```
