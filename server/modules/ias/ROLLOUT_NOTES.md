# IAS (Integral Ad Science) connector — rollout notes

Module landed to the epic-25 industrial standard (catalog + error taxonomy +
account topology + conformance). **Live ratification is deferred (AI-13):** we
have no IAS Signal test account, and the official API reference is login-gated,
so the module ships `public_catalog.verification: blocked` and is proven against
the real API by `scripts/ratify_connector.py` the moment a client account
connects.

## What is confirmed (official IAS sources)

- **Product / API.** IAS Signal; the data surface is the **IAS Reporting API**
  (RESTful, GET-only, synchronous). Base URL `https://api.integralplatform.com`.
  Refs: `https://reporting.integralplatform.com/help/rs/home.html`,
  `https://helpcenter.integralplatform.com/article/reporting-api`.
- **Auth.** OAuth 2.0 **password grant**. Token endpoint
  `https://api.integralplatform.com/uaa/oauth/token` (HTTP Basic
  `client_id:client_secret` + `username`/`password` in the body,
  `grant_type=password`). The `access_token` is sent as `Authorization: Bearer`
  and reused until it expires. `client_id`/`client_secret` come from an IAS rep
  (not self-service) → `auth_type: oauth2`. The connector obtains the token via
  `nango_client.get_fresh_token(connection_id, provider="ias")` (AD-3).
- **Reporting entity.** The **Team** (`/reportingservice/api/teams/{teamId}/...`).
  A credential can reach multiple Teams; `discover_accounts` enumerates them.
  Topology `selection_level: team`. No `IAS_TEAM_ID` in production (interim env
  fallback only, mirroring gsc's `GSC_SITE_URL` deprecation path).
- **Product split.** `platform` path code: `CM` = Campaign Management
  (advertiser/agency, default) vs `FW` = Firewall.
- **Expired token.** OAuth `invalid_token` / "Access token expired" on HTTP 401
  → the pure-HTTP taxonomy routes it to `auth_expired` (reconnect). See the
  manifest `_error_map_note` for why the error_map is empty.

## Load-bearing UNKNOWNS to confirm at first live ratification (marked ASSUMED in code)

These are the only gaps that block a working pull; everything in the catalog is
tiered and diffable, and each is proven by a real request during ratification:

1. **Date-range query-parameter names & format.** Assumed `startDate` / `endDate`
   (`YYYY-MM-DD`) in `pull()`. Confirm the exact param names.
2. **Metric/dimension selection params.** Assumed comma-joined `metrics=` /
   `dimensions=` using the camelCase source tokens in `api_catalog.json`. Confirm
   the selector param names and that the wire tokens match the catalog
   `source_field` values (Funnel-derived; see below).
3. **Response envelope.** Assumed rows under `rows` (fallback `data`). Confirm.
4. **Discovery endpoint.** Assumed `GET /reportingservice/api/teams` returning a
   `teams` array of `{id, name}`. Confirm the path and shape.
5. **Rate limits / pagination.** Undocumented publicly. Quota set conservatively;
   429 → `RateLimitError`. Confirm the real limits and any paging model.

## Catalog provenance (honesty note)

`catalog_sources/official_fields.json` is a **curated authority snapshot**. The
official reference is a login-gated SPA that is not statically fetchable, so the
field set was assembled from IAS's own documented measurement families
(viewability, invalid traffic / IVT, brand safety & suitability, risk categories,
quality impressions, attention, video, carbon) and the camelCase `source_field`
tokens follow the **Funnel** connector's naming — the closest public proxy for
the raw IAS API columns — cross-checked against **Adverity** and **Supermetrics**.
The exact wire field names are therefore SUSPECTS until the ratification field
probe confirms them; that is precisely what keeps `verification: blocked`.

Exposure policy is `manifest`: the six additive count metrics + three structural
dimensions in `manifest.source_capabilities.fields` are `exposed` (extracted by
the three exact_bundle daily profiles); every other cataloged field is `planned`.
Ratio/rate and index (attention) metrics stay `planned` on purpose — they are
recomputed downstream from their additive numerator/denominator counts, never
summed (AD-4).

## Follow-ups (not blocking)

- **Report packs.** `reports/*.json` packs are intentionally omitted until the
  canonical dbt dictionary (`core.report_dictionary`) carries IAS metric names;
  report-pack semantic validation keys on that dictionary. Add once present.
- **Widen extraction.** Promote `planned` families (risk categories, IVT-by-type,
  video quartiles, attention) to `exposed` as new profiles/grains land.
- **Ratification.** Run `scripts/ratify_connector.py --module ias --connection
  <id> --account <teamId> --tier core` against a real account, then flip the
  manifest `public_catalog.verification.status` to `ratified`.
