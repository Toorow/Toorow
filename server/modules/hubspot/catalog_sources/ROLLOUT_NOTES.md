# HubSpot connector ? ratification notes (Story 33.2)

## Deterministic catalog baseline

- Official fields: **88**.
- Exposed: **46**.
- Excluded: **42**.
- Planned: **0**.
- Drift ids: **0**.
- Selected CRM Search API: **v3**. The `2026-03` builder is retained for bounded live parity only.

`build_official_fields.py` and `scripts/build_api_catalog.py` are deterministic. The
source ledger, generated catalog and manifest all pin `v3`; a live portal is required
before changing that selection. Account metadata intentionally uses the independent
`/account-info/2026-03/details` endpoint.

## Extraction correctness

Daily queries use timezone-correct half-open `GTE`/`LT` windows. Search windows that
reach the 10,000-object boundary split recursively and deduplicate object ids; an
unsplittable window raises an incomplete-search error and publishes nothing. Closed-won
deals use `hs_is_closed_won=true`, independent of custom pipeline stage identifiers.
`deal_amount` sums `amount_in_home_currency` and carries the portal company currency
through raw, staging and mart output. The refetch ladder is 3/14/45 days.

## Error normalization and privacy

The connector copies HubSpot's `category` into a local `code` for manifest error-map
classification while retaining the original sanitized body. HTTP fallback remains for
category-free or malformed responses. Contact/deal properties are neither logged nor
placed in the default KPI aggregates; catalog-selected properties remain isolated in
the long-format raw catalog table. CRM metrics remain isolated from ad-platform claims.

## Verification state

Public verification remains `blocked` with reason `live_evidence_not_ratified`. A real
portal pass must still prove OAuth, account metadata, v3/2026-03 parity, custom-pipeline
closed-won behavior, timezone day boundaries, company currency, live properties drift
and sanitized golden fixtures. No mocked or static result may flip that state.
