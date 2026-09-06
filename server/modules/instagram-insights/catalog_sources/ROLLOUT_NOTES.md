# Instagram Insights rollout notes

## Implemented contract

- Instagram Login against `graph.instagram.com`, pinned to Graph API `v24.0`.
- OAuth scopes: `instagram_business_basic` and
  `instagram_business_manage_insights`.
- Professional Business and Creator accounts only.
- `account_daily`: `reach` and `profile_views`, maximum 90-day history.
- `media_performance`: cumulative `shares` and `comments` for owned Posts and
  Reels published in the requested window. Stories are intentionally excluded.
- Empty insight datasets and missing metrics remain absent, never fabricated as
  zero.

## Live gate

The public verification state remains `blocked`. Before changing it, run a live
ratification with an external professional account and capture:

1. Nango's Instagram Login provider configuration and granted scopes.
2. `/me` discovery for Business and Creator accounts.
3. Account insight time-window boundaries, pagination and a metric hidden by the
   100-follower eligibility rule.
4. Media pagination across Feed, Carousel and Reels content.
5. A real empty dataset, permission failure, expired/revoked token and HTTP 429.
6. Report-day timezone evidence. Until the provider exposes it, the connector
   deliberately emits a timezone gap.

Regenerate the catalog with:

```powershell
uv run python scripts/build_api_catalog.py --module instagram-insights --sources-dir server/modules/instagram-insights/catalog_sources --out server/modules/instagram-insights/api_catalog.json --report server/modules/instagram-insights/catalog_sources/fusion-report.json
```
