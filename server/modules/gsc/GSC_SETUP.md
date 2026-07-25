# Google Search Console Module — Real E2E Setup

## Human Gates

This module requires the following human gate steps for real E2E operation:

### HG-B (inherited): Google OAuth Client
The GSC module uses the same Google OAuth client as GA4 (HG-B: Create Google OAuth Client).
The `https://www.googleapis.com/auth/webmasters.readonly` scope must be added to the
existing OAuth client app in Google Cloud Console.

To add the scope:
1. Open the Google Cloud Console OAuth client for the toorow project.
2. Under "Authorized scopes", add: `https://www.googleapis.com/auth/webmasters.readonly`
3. Re-publish the OAuth consent screen if required.
4. The Nango `gsc` provider integration must be created with this scope.

### GSC Property Setup
- Site URL format: `https://example.com/` (URL prefix) or `sc-domain:example.com` (domain)
- The URL format in GSC must exactly match the format used in API calls.
- Verify ownership of the property in Google Search Console before connecting.

### Nango Provider Integration
Create a Nango integration named `gsc` (or reuse the Google integration with an added scope):
- Provider: Google
- Scopes: `openid email https://www.googleapis.com/auth/webmasters.readonly`
- This is an ADDITIVE scope — the same Nango integration can serve both GA4 and GSC
  if the OAuth client covers both scopes. Otherwise, create a separate `gsc` integration.

Recommendation: use a single `google` Nango integration covering both GA4 and GSC
(`analytics.readonly` + `webmasters.readonly` scopes). Less credential management,
fewer Nango connections per user. Document both scope additions to Jean's HG-B task.

## API Notes
- GSC Search Analytics API: `POST /webmasters/v3/sites/{siteUrl}/searchAnalytics/query`
- Rate limit: 1,200 queries / 100 seconds (per the connector manifest quota block)
- Row limit per query: 25,000 (declared in manifest `extraction_capabilities.row_limit`)
- Regex filters: supported via `dimensionFilterGroups` with `type: REGEX`

## Field Mapping Notes
- GSC returns `position` — the connector maps this to `average_position` (canonical name).
- GSC returns `ctr` — this is DISCARDED at ingest (ratio metric, computed at view time).
- Device values from GSC are UPPERCASE (`MOBILE`, `DESKTOP`, `TABLET`) — the connector
  normalizes these to lowercase at ingest (`_DEVICE_CANONICAL_MAP` in connector.py).

## Non-Additive Metric: average_position
- `average_position` is NON-ADDITIVE (AD-4). Never sum it directly.
- It is stored raw per row in `raw_gsc_daily` and `fact_daily_kpi`.
- The `semantic_avg_position` dbt view applies impression-weighted aggregation:
  `SUM(position * impressions) / SUM(impressions)`.
- This is the ONLY correct aggregation path. The CI guard (`make check-non-additive-guard`)
  enforces this by blocking `SUM(average_position)` in all mart SQL files.

## Local Development (Mocked)
All automated tests use respx mocks — no real credentials required.
See `server/modules/gsc/tests/` for golden fixtures and `server/tests/modules/gsc/` for test suite.
