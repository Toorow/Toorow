# Square — Rollout Notes

## Field counts

| Object / section | Metrics | Dimensions | Total |
|---|---|---|---|
| PAYMENT | 6 | 16 | 22 |
| PROCESSING FEE | 1 | 2 | 3 |
| CARD DETAILS | 0 | 8 | 8 |
| LOCATION | 0 | 5 | 5 |
| ORDER | 1 | 2 | 3 |
| REFUND | 1 | 3 | 4 |
| CUSTOMER | 0 | 3 | 3 |
| PLATFORM CONTRACT | 3 | 0 | 3 |
| **Total** | **12** | **39** | **51** |

**Official snapshot fields (official_fields.json):** 51 fields across the Square Connect v2
Payment object and adjacent objects. Authoritative counts live in `fusion-report.json`.

**Fetchable (exposure=exposed):** PAYMENT + PROCESSING FEE + CARD DETAILS + PLATFORM CONTRACT
= 36 fields — everything reachable from a single `GET /v2/payments` response (the Payment
object plus its embedded `processing_fee[]` and `card_details`).

**Excluded (exposure=excluded):** LOCATION + ORDER + REFUND + CUSTOMER = 15 fields — each
requires a separate endpoint (ListLocations, Orders API, ListRefunds, RetrieveCustomer) not
called by the daily pull. Honest reasons in `catalog_sources.json.excluded_sections`.

**Manifest exposed fields (existing connector):** 9 fields — 5 metrics
(revenue/refunds/fees/transaction_count/order_count) and 4 dimensions
(date/payment_id/order_id/location_id). All 9 are present in `official_fields.json` with
matching kind and source_field, so `drift_ids` is empty.

## Platform-contract / official-name divergence

The platform ids `revenue`/`refunds`/`fees` map to Square tokens:
- `revenue` → `payment.amount_money.amount`
- `refunds` → `payment.refunded_money.amount`
- `fees`    → SUM(`payment.processing_fee[].amount_money.amount`)

These are carried from the module manifest (`source_field` = `amount`/`refunded`/`fee_amount`,
the connector's parsed source names). The live probe adjudicates the divergence.

## Error map decision

Square error responses use a LIST shape: `{"errors":[{"category","code","detail","field"}]}`
(category ∈ AUTHENTICATION_ERROR | INVALID_REQUEST_ERROR | RATE_LIMITED | API_ERROR). This
differs from the `{error:{code}}` shape `core._extract_provider_code` recognises, so the
connector NORMALISES the body before `classify_http_error` (`_square_error_payload`): it
surfaces the first error's `code` at the top level while preserving the original `errors`
array as evidence. The manifest `error_map` then refines:
- `401:ACCESS_TOKEN_EXPIRED` → `auth_expired` (refreshable)
- `401:ACCESS_TOKEN_REVOKED` → `auth_revoked` (credential gone) — the key distinction, since
  both return 401 and would otherwise both fall back to `auth_expired`
- `401:CLIENT_DISABLED` → `auth_revoked`
- `403:INSUFFICIENT_SCOPES` / `403:FORBIDDEN` → `permission_denied`
- `400:BAD_REQUEST` / `400:VALUE_TOO_*` → `invalid_request`
- `500:INTERNAL_SERVER_ERROR` → `provider_transient`

`RATE_LIMITED` (429) never reaches `classify_http_error` — the connector raises
`core.quota.RateLimitError` on 429.

## Account topology decision

Square access is seller-scoped: one OAuth access token (or Personal Access Token) reaches ALL
of a seller's LOCATIONS. `account_topology` is DECLARED as a single flat level `location`
(`selection_level='location'`), and `discover_accounts()` calls `GET /v2/locations` to list
them. The selected `location_id` is passed to `pull()` as the `/v2/payments` `location_id`
filter. NEVER a location in an env var (mirrors meta-ads 25.5).

## Orchestrator command block

```bash
# Copy catalog_sources into a scratch sources dir (official source is self-contained,
# no network fetch required).
mkdir -p /tmp/roll-square
cp server/modules/square/catalog_sources/catalog_sources.json /tmp/roll-square/
cp server/modules/square/catalog_sources/official_fields.json /tmp/roll-square/

# Generate api_catalog.json
uv run python scripts/build_api_catalog.py \
  --module square \
  --sources-dir /tmp/roll-square \
  --report server/modules/square/catalog_sources/fusion-report.json

# Verify drift is empty, then copy to module
cp /tmp/roll-square/api_catalog.json server/modules/square/api_catalog.json

# Run conformance gate
CATALOG_GATE_MODE=fail uv run pytest server/tests/conformance/test_api_catalog.py -k square -q

# Regenerate public registry
uv run python scripts/export_connector_registry.py
```

## Live ratification (deferred)

No Square test/sandbox account is currently available (see memory `no-connector-test-accounts`).
The 25.6 live probe and AI-13 live-integration pass are therefore **blocked**;
`public_catalog.verification.status = "blocked"` (`reason_code = live_evidence_not_ratified`).
Dev, review, and central conformance are NOT blocked. To confirm in a live pass: exact Payment
shape, `processing_fee` availability before settlement, `order_id` presence rate per channel
(POS vs Online Checkout), real 429/Retry-After behaviour, and the minimal OAuth scopes.
