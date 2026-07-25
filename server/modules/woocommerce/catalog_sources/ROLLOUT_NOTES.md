# WooCommerce catalog — rollout notes

**API pinned:** WooCommerce REST API **v3** — base `https://{store}/wp-json/wc/v3`.
Authority: <https://woocommerce.github.io/woocommerce-rest-api-docs/> (verified 2026-07-21).

## Generation command

```bash
uv run python scripts/build_api_catalog.py \
    --module woocommerce \
    --sources-dir server/modules/woocommerce/catalog_sources \
    --out server/modules/woocommerce/api_catalog.json \
    --report server/modules/woocommerce/catalog_sources/fusion-report.json
```

`official_fields.json` is curated by hand from the Order object reference; the
generator fuses it with the manifest's PLATFORM CONTRACT fields and tiers each
field via `section_tier_map` + `field_tier_overrides`. `drift_ids` MUST be empty.

## Field counts / accounts

- Single-source catalog: the WooCommerce Order object is fully documented on one
  reference page, so there is no Supermetrics enrichment layer (Supermetrics has
  no WooCommerce connector). `enrichment_only` suspects: none.
- Sections: ORDER, REFUND, BILLING ADDRESS, SHIPPING ADDRESS, LINE ITEM, PRODUCT,
  COUPON, FEE, TAX, PLATFORM CONTRACT — all reachable from a single
  `GET /wc/v3/orders` payload (no extra endpoint needed).

## Platform-contract divergences (adjudicated by the live probe)

- `revenue` ← `order.total` (grand total, additive).
- `refund_amount` ← `abs(sum(order.refunds[].total))`. WooCommerce reports refund
  totals as **NEGATIVE** amounts; the connector takes `abs()` **explicitly** and
  stores a DEDICATED positive column — NEVER subtracted silently from revenue
  (decision REFERENCE shopify 15.4 / stripe 15.7).
- `orders_count` ← 1 per order (synthetic, `source_field: "order"`).
- `date` ← `order.date_created` (site-timezone day grain; `date_created_gmt` is an
  alternative to confirm live if intraday tz boundary matters).
- `transaction_id` ← `order.transaction_id`. **Often empty** on manual/offline
  payments → GA4 × WooCommerce join is partial (measured by a ≥X% presence test,
  mirrors stripe `client_reference_id` 15.7). Not assumed present.

## Sales-of-record decision (Jean 2026-07-21)

Only `status ∈ {completed, processing}` counts as a sale. The `pull` sends
`status=completed,processing` to `/orders`; `on-hold` / `pending` / `failed` /
`cancelled` / `trash` are never landed as revenue.

## Ratification

`verification: blocked` until a live probe runs. Unlike paid-media connectors, a
**local WordPress + WooCommerce install with demo data** is a valid ratification
target — no client account required. Run `scripts/ratify_connector.py --module
woocommerce` once such a store's consumer key/secret is connected via Nango
(Basic auth integration).
