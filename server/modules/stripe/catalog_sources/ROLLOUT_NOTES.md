# Stripe — Rollout Notes (Story 25.7)

## Field counts

| Object | Metrics | Dimensions | Total |
|---|---|---|---|
| CHARGE | 7 | 33 | 40 |
| BALANCE TRANSACTION | 3 | 8 | 11 |
| CUSTOMER | 1 | 8 | 9 |
| INVOICE | 10 | 14 | 24 |
| SUBSCRIPTION | 2 | 12 | 14 |
| REFUND | 1 | 7 | 8 |
| PAYOUT | 2 | 14 | 16 |
| **Total** | **26** | **96** | **122** |

**Official snapshot fields (official_fields.json):** 122 fields across 7 Stripe API objects.

**Supermetrics reference (enrichment-only, not emitted):** 62 metrics / 206 dimensions.
Supermetrics covers a narrower slice (CHARGE + BALANCE TRANSACTION + CUSTOMER +
APPLICATION FEE + COUPON) with pre-computed aggregates (transaction_charges,
transaction_average_net, etc.) that are computed from raw charge/balance data rather
than being direct API object fields. These enrichment-only fields are documented here
but excluded from official_fields.json because Supermetrics is never the authority per
the playbook.

**Manifest exposed fields (existing connector):** 9 fields — 5 metrics
(revenue/refunds/fees/transaction_count/order_count) and 4 dimensions
(date/charge_id/payment_intent_id/client_reference_id). All 9 are present in
official_fields.json with matching kind and source_field.

## Enrichment-only justification

Fields present in Supermetrics but absent from the Stripe API objects:
- `transaction_charges`, `transaction_refunds`, `transaction_fees` — computed counts
  derived from BalanceTransaction listings, not an object field
- `charge_fee_percent`, `transaction_charge_percent` — derived ratios
- `balance_source_card`, `balance_source_bank` — balance sub-components not in
  the standard BalanceTransaction object (from the Stripe Balance API endpoint,
  a different resource)
- `app_fee_id`, `app_fee_amount_refunded` — ApplicationFee object (Connect only,
  out of scope for direct merchant integration)
- `coupon_id`, `coupon_duration`, `coupon_percent_off`, etc. — Coupon object
  (not in the primary reporting objects scope)

These fields would require additional endpoint calls (Balance, ApplicationFee, Coupon
objects) and are deferred to a future widening iteration.

## Error map decision

Stripe uses 4 error types (`api_error`, `card_error`, `idempotency_error`,
`invalid_request_error`) and carries `error.code` in its JSON response body.
The `core._extract_provider_code` function recognises the `{error:{code:X}}` shape
and extracts Stripe's code string.

The manifest error_map refines:
- `401:api_key_expired` → `auth_expired` (stale key)
- `401:platform_api_key_expired` → `auth_expired` (Connect platform key stale)
- `401:invalid_api_key` → `auth_revoked` (wrong/revoked key)
- `401:secret_key_required` → `auth_revoked` (public key used where secret needed)
- `403:permission_error` → `permission_denied`
- `400:parameter_*` → `invalid_request`
- `500:api_error` → `provider_transient`

The pure-HTTP fallback (401 → auth_expired without a recognised code) is already
correct for Stripe in the general case, so the map does not meaningfully diverge
from the HTTP baseline — it adds specificity for the most actionable cases.

## Account topology decision

Stripe API-key access is merchant-scoped: one secret key = one merchant account.
No account discovery call is required. Stripe Connect (OAuth, sub-accounts) is out
of scope for this integration (direct merchant model only). `account_topology` is
therefore not declared; the decision is documented in `_account_topology_note` in
manifest.json.

## Orchestrator command block

```bash
# Fetch Supermetrics enrichment snapshot (not committed — enrichment only)
curl -sL https://docs.supermetrics.com/docs/stripe-fields.md \
  -o /tmp/roll-stripe/supermetrics.md

# Copy catalog_sources into the sources dir
cp server/modules/stripe/catalog_sources/catalog_sources.json /tmp/roll-stripe/
cp server/modules/stripe/catalog_sources/official_fields.json /tmp/roll-stripe/

# Generate api_catalog.json
uv run python scripts/build_api_catalog.py \
  --module stripe \
  --sources-dir /tmp/roll-stripe \
  --report /tmp/roll-stripe/fusion-report.json

# Verify drift is empty, then copy to module
cp /tmp/roll-stripe/api_catalog.json server/modules/stripe/api_catalog.json

# Run conformance gate
CATALOG_GATE_MODE=fail uv run pytest server/tests/conformance/test_api_catalog.py \
  -k stripe -q

# Run module tests (includes new error_map tests)
uv run pytest server/tests/modules/stripe/ -q

# Regenerate public registry
uv run python scripts/export_connector_registry.py
```

## Review corrections (2026-07-21, fresh-context curation review)

- **Counts correction**: the per-object table above is stale — the committed
  snapshot has **139 fields** (after review fix F-2), not 122. Authoritative
  counts live in `fusion-report.json` (official_total).
- **F-2 platform-contract dedup**: the raw `amount` / `amount_refunded` /
  `fee_amount` planned fields were REMOVED — their families are owned by the
  platform-contract fields `revenue` / `refunds` / `fees` (same source_field,
  HubSpot pattern). Keeping both would have exposed the same Stripe token twice
  and invited double-counting. **Official-name divergence**: the platform ids
  `revenue`/`refunds`/`fees` map to Stripe tokens `amount`/`amount_refunded`/
  `fee_amount` — the 25.6 live probe adjudicates.
