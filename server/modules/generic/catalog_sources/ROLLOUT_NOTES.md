# Rollout Notes — generic connector (Story 25.7)

## Field counts

| Kind      | Count | Notes                                                          |
|-----------|-------|----------------------------------------------------------------|
| dimension | 1     | `date` — sole field declared in manifest source_capabilities  |
| metric    | 6     | `cost`, `revenue`, `conversions`, `clicks`, `impressions`, `sessions` — full allowed_targets list from manifest field_discovery |
| **total** | **7** |                                                                |

## Decisions

**Self-declared contract.** The generic module has no external provider. The
authoritative source is `connector.py` itself: the module-level docstring (lines
1–30) and `source_capabilities.field_discovery.allowed_targets` in
`manifest.json`. `official_fields.json` is curated from those two sources, not
fetched from any network endpoint.

**No enrichment.** No Supermetrics catalog exists for this module. Enrichment
field `_fetch` commands are omitted from `catalog_sources.json`.

**Metric source_fields equal field_id.** The generic contract accepts any source
column name, remapped to a canonical target via `connection_config['mappings']`.
The six metric `source_field` values in `official_fields.json` are the canonical
target names from `allowed_targets`; they represent the _landing name_ in
`raw_generic_daily.metric`, not a literal source column name. This is honest:
that is what the contract documents.

**Single section GENERIC, tier core.** All seven fields belong to `GENERIC/core`.
The module has no cost/impression/click tiering hierarchy of its own — every
declared field is a day-one primitive.

**error_map absent by design.** Transport errors (HTTP non-200, SSRF rejection,
redirect refusal, unresolvable host) raise `RuntimeError` or `ValueError` with
French messages. There are no provider error codes. A `_error_map_note` in
`manifest.json` documents this.

**account_topology absent by design.** `auth_type='none'`; no provider, no
account hierarchy. A `_account_topology_note` in `manifest.json` documents this.

**Drift check.** The sole manifest field `date` (dimension / `runtime_date_column`)
appears in `official_fields.json` with matching `kind` and `source_field`. Drift
is empty.

## Orchestrator command block

```bash
# Simulate the catalog build (no network required — self-declared contract).
uv run python scripts/build_api_catalog.py \
  --module generic \
  --sources-dir /tmp/roll-generic \
  --report /tmp/roll-generic/report.json

# Copy sources into the staging dir first:
mkdir -p /tmp/roll-generic
cp server/modules/generic/catalog_sources/catalog_sources.json /tmp/roll-generic/
cp server/modules/generic/catalog_sources/official_fields.json /tmp/roll-generic/

# Gate check (after api_catalog.json is generated and committed):
CATALOG_GATE_MODE=fail uv run pytest server/tests/conformance/test_api_catalog.py -q -k generic
uv run pytest server/tests/modules/generic/ -q
```
