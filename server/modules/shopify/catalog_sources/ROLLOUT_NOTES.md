# Shopify — Rollout Notes (Story 25.7)

## Field counts

| Source | Metrics | Dimensions | Total |
|--------|---------|------------|-------|
| Official (Shopify Admin REST Order) | 14 | 45 | 59 |
| Supermetrics enrichment | 21 | 134 | 155 |

Official field breakdown by section:

| Section | Metrics | Dimensions |
|---------|---------|------------|
| ORDER | 8 | 25 |
| REFUND | 2 | 3 |
| LINE ITEM | 3 | 5 |
| PRODUCT | 0 | 3 |
| CUSTOMER | 2 | 5 |
| CUSTOMER GEO | 0 | 3 |
| BILLING ADDRESS | 0 | 5 |
| SHIPPING ADDRESS | 0 | 5 |
| TRAFFIC SOURCE | 0 | 2 |

All 5 existing manifest `source_capabilities.fields` are present in `official_fields.json`
with matching `kind` and `source_field`:

| manifest field_id | source_field | kind |
|-------------------|--------------|------|
| `revenue` | `total_price` | metric |
| `refund_amount` | `total_refunded` | metric |
| `orders_count` | `order` | metric |
| `date` | `created_at` | dimension |
| `transaction_id` | `transaction_id` | dimension |

Drift check: 0 drift fields expected (all manifest fields are in the official catalog).

## error_map decision

Shopify Admin REST uses pure HTTP status semantics:

- `401` → Unauthorized (expired/invalid token) → `auth_expired`
- `402` → Payment Required (shop suspended) → `permission_denied`
- `403` → Forbidden (scope missing) → `permission_denied`
- `423` → Locked (shop locked) → `provider_transient`
- `429` → Too Many Requests → `RateLimitError` (breaker path, not in error_map)

No provider-level numeric error codes exist that refine beyond the HTTP status.
`error_map` is intentionally empty `{}`. This is documented in `manifest.json`
as `_error_map_note`. The pure-HTTP classification in `core.pull_errors` already
handles these status codes correctly without a refinement map.

## account_topology decision

Shopify is shop-scoped: one OAuth token grants access to exactly one merchant store.
There is no account hierarchy (no MCC, no business manager, no advertiser list).
The `shop_domain` is the sole scope and is resolved from the Nango credential at pull
time (passed in the queue dispatch, not selected by the user from a topology endpoint).
`account_topology` is absent from `manifest.json` by design; this is documented as
`_account_topology_note`.

## enrichment_only justification

Supermetrics computes the following fields by joining across multiple Shopify API
resources (inventory_items, refund_line_items, order adjustments) or applying
arithmetic across the order payload:

- `gross_profit` = net_sales - cost_of_goods_sold (requires inventory_item cost)
- `cost_of_goods_sold` (requires inventory_item cost per variant)
- `ordered_quantity`, `returned_quantity`, `net_quantity` (line_item + refund_line_item aggregation)
- `units_per_transaction` = net_quantity / orders (computed ratio)
- `avg_total_sales` = total_sales / orders (computed ratio)
- `customer_count`, `customer_lifetime_duration` (customer API object, not on order)
- `product_inventory_value`, `product_inventory_quantity` (inventory API, not on order)

These are enrichment_only suspects — they cannot be derived from the Order REST
payload alone and are not present in `official_fields.json`.

## Orchestrator command block

```bash
# 1. Fetch the Supermetrics enrichment snapshot (local only, do not commit)
curl -sL https://docs.supermetrics.com/docs/shopify-fields.md \
  -o /tmp/roll-shopify/supermetrics.md

# 2. Copy catalog_sources.json and official_fields.json into the sources dir
cp server/modules/shopify/catalog_sources/catalog_sources.json /tmp/roll-shopify/
cp server/modules/shopify/catalog_sources/official_fields.json /tmp/roll-shopify/

# 3. Run the catalog generator
uv run python scripts/build_api_catalog.py \
  --module shopify \
  --sources-dir /tmp/roll-shopify \
  --report /tmp/roll-shopify/report.json

# 4. Review fusion report — drift_ids MUST be empty
cat /tmp/roll-shopify/report.json | python -c "import json,sys; r=json.load(sys.stdin); print('drift:', r.get('drift_ids',[])); print('enrichment_only:', len(r.get('enrichment_only_ids',[])))"

# 5. Run conformance gate
CATALOG_GATE_MODE=fail uv run pytest server/tests/conformance/test_api_catalog.py -q -k shopify

# 6. Run module tests
uv run pytest server/tests/modules/shopify/ -q

# 7. Regenerate public registry
uv run python scripts/export_connector_registry.py
```
