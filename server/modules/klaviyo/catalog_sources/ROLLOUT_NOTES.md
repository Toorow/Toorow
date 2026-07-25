# Klaviyo Catalog Sources — Rollout Notes (Story 25.7, 2026-07-21)

## Counts

| Layer | Count | Notes |
|---|---|---|
| Official statistics (metrics) | 30 | From Klaviyo OpenAPI stable: query_campaign_values.json + query_flow_values.json |
| Official grouping dimensions (campaign endpoint) | 11 | campaign_id, campaign_message_id, campaign_message_name, group, group_name, send_channel, tag_id, tag_name, text_message_format, variation, variation_name |
| Official grouping dimensions (flow endpoint) | 10 | flow_id, flow_message_id, flow_message_name, flow_name, send_channel, tag_id, tag_name, text_message_format, variation, variation_name |
| Derived dimensions in official_fields.json | 1 | date (from series endpoint daily interval) |
| Total rows in official_fields.json | 46 | 30 metrics + 16 unique dimensions (deduplicating shared grouping fields across endpoints) |
| Supermetrics enrichment (metrics) | 83 | From https://docs.supermetrics.com/docs/klaviyo-fields.md header |
| Supermetrics enrichment (dimensions) | 119 | From same source |

## Drift Findings (HARD — must resolve before live ratification)

Three hard drifts were found between the manifest's `source_capabilities.fields` and the official Klaviyo Reporting API OpenAPI spec:

1. **`sends` / `source_field: sends`** — The statistic `sends` does NOT exist in the Klaviyo OpenAPI. The correct official name is `recipients`. The connector's `_REPORTING_STATISTICS` list and the manifest must be updated to use `recipients`. The canonical `sends` field_id can be preserved with `source_field: recipients`.

2. **`attributed_revenue` / `source_field: revenue`** — The statistic `revenue` does NOT exist in the Klaviyo OpenAPI. The correct official name is `conversion_value`. The connector's `_REPORTING_STATISTICS` list must be updated.

3. **`campaign_name` / `source_field: campaign_name`** — `campaign_name` is not a grouping dimension on the `campaign-values-reports` endpoint. Available campaign grouping fields are `campaign_id`, `campaign_message_id`, `campaign_message_name`, etc. The campaign name must be resolved by joining to GET `/api/campaigns/{id}` (separate lookup, not a reporting statistic). The field can remain in the catalog as `exposure: planned` pending a follow-on story implementing the join.

## Enrichment-Only Justification

The following Supermetrics sections are classified as `advanced / enrichment_only` because they require API endpoints NOT covered by the Klaviyo Reporting API (campaign-values-reports / flow-values-reports):

- **MAGENTO, WOOCOMMERCE, STRIPE** — Cross-source platform purchase metrics (e.g. `magento_placed_order`, `shopify_placed_order_value`). These are Klaviyo metric-event tracking of third-party platform purchases, available via the Metrics API (`/api/metric-aggregates/`) with event-property filtering, not via the Reporting API. Extraction requires a separate pull path per integration.
- **PERSON** — Profile-level attributes (email, city, country, etc.) from the Profiles API (`/api/profiles/`). Not available as reporting statistics.
- **PURCHASE** — Order/event dimensions (order_number, gateway, currency) from the Events API. Requires metric-aggregates endpoint with property breakdown, not Reporting API.
- **LIST** — List membership metrics from the Lists API. Different endpoint, different pull path.
- **AD** — Ad platform attribution dimensions stored as UTM properties on profiles.

These sections will generate `enrichment_only: true` fields in the api_catalog.json, which are suspects to verify against the official doc before promoting.

## Error Map Decision

Klaviyo uses JSON:API error format. The `code` field in error objects is a free-text string (e.g. `"invalid"`, `"not_found"`) — not an enumerated integer. No finite set of numeric sub-codes is published. Therefore the `error_map` in `manifest.json` keys on HTTP status only (no `:provider_code` refinement). The pure-HTTP classification in `core.pull_errors._base_class_for_status` already handles all cases correctly:
- 401 → `auth_expired`
- 403 → `permission_denied`
- 400 / 422 → `invalid_request`
- 500 / 503 → `provider_transient`

## Account Topology Decision

Klaviyo is API-key scoped — one key, one account, no hierarchy. `account_topology` block is omitted; `_account_topology_note` is present in `manifest.json`.

## Orchestrator Command Block

```bash
# 1. Fetch enrichment snapshot (do NOT commit this file)
curl -sL https://docs.supermetrics.com/docs/klaviyo-fields.md \
  -o /tmp/roll-klaviyo/supermetrics.md

# 2. Copy catalog_sources into the sources dir
cp server/modules/klaviyo/catalog_sources/catalog_sources.json /tmp/roll-klaviyo/
cp server/modules/klaviyo/catalog_sources/official_fields.json /tmp/roll-klaviyo/

# 3. Run the catalog generator
uv run python scripts/build_api_catalog.py \
  --module klaviyo \
  --sources-dir /tmp/roll-klaviyo \
  --report /tmp/roll-klaviyo/report.json

# 4. Review the fusion report for drift_ids (must be empty after resolving the 3 hard drifts above)
cat /tmp/roll-klaviyo/report.json | python -m json.tool | grep -A20 '"drift_ids"'

# 5. Run conformance + module tests
CATALOG_GATE_MODE=fail uv run pytest server/tests/conformance/test_api_catalog.py -q
uv run pytest server/tests/conformance/ server/tests/modules/klaviyo/ -q

# 6. Regenerate public registry
uv run python scripts/export_connector_registry.py
```

## Pre-Rollout Story Required

Before running the orchestrator command, the following must be fixed in `connector.py` and `manifest.json` (the 3 hard drifts above):
1. Replace `"sends"` with `"recipients"` in `_REPORTING_STATISTICS` and update the manifest `source_field`.
2. Replace `"revenue"` with `"conversion_value"` in `_REPORTING_STATISTICS` and update the manifest `source_field`.
3. Remove `campaign_name` from `source_capabilities.fields` (or mark as `exposure: planned`) pending a join-lookup implementation.
