# Rollout Notes — google-sheets catalog_sources (story 25.7)

## Counts

| Artifact | Count |
|---|---|
| Total fields in contract | 5 |
| Metrics | 3 (`budget_declared`, `target_revenue`, `target_conversions`) |
| Dimensions | 2 (`date`, `sheet_row_id`) |
| Sections | 1 (`TABULAR`) |
| Tiers | all `core` |
| Official sources | 1 (Sheets API v4 transport reference) |
| Enrichment sources | 0 |
| drift_ids | 0 (self-declared contract — drift is impossible by definition) |

## Self-declared contract rationale

Google Sheets is a **user-defined tabular source**: the Sheets API does not
publish a field catalog. The `values.get` / `values:batchGet` endpoint returns
whatever columns the user has placed in their spreadsheet, identified only by
their header row.

The toorow tabular-objectives contract v1 defines **five canonical fields**:

- `budget_declared`, `target_revenue`, `target_conversions` — the three KPI
  objective metrics that any plan-vs-actual workflow requires.
- `date` — the time grain dimension (ISO-8601 string from the user's date
  column).
- `sheet_row_id` — the breakdown dimension (user's channel/campaign column, or
  a synthetic `row_N` index when no `row_id_column` is declared).

These five fields are the **closed, minimal contract by design**. Users declare
their own column names in the datastream `column_mapping`; the connector
translates them to these canonical names at pull time (`_validate_column_mapping`
+ `_parse_sheet_row` in `connector.py`). Adding a column to a user's sheet does
not expand this contract — it expands only if a new canonical field is added to
the product spec (a story, not a catalog regeneration).

The transport URL recorded in `catalog_sources.json`
(`https://developers.google.com/sheets/api/reference/rest/v4/spreadsheets.values`)
documents the HTTP API used to read the sheet. It is NOT a field reference.

## Error map decision

A formal `error_map` dict is omitted. Sheets has no provider error-code
registry comparable to Meta subcodes or Google Ads `error.errors[].reason`.
The three dedicated raise sites in `connector.py` cover the full error surface:
- HTTP 429 → `core.quota.RateLimitError` (breaker path, direct raise)
- HTTP 403 → Python `PermissionError` (missing scope / revoked consent)
- HTTP 404 → Python `FileNotFoundError` (spreadsheet not found)
- All other non-2xx → `httpx.resp.raise_for_status()`

The `_error_map_note` in `manifest.json` records this decision.

## Account topology decision

No `account_topology` block declared. Spreadsheet selection is datastream-level
configuration (`spreadsheet_id`, `sheet_range`, `column_mapping` are pull
parameters). There is no sub-account or property hierarchy to discover or
select at connection time. The `_account_topology_note` in `manifest.json`
records this decision.

## Orchestrator command block

```bash
# Run catalog generator (no-op for self-declared modules — sources-dir is
# consumed as-is; no fetch commands to execute).
uv run python scripts/build_api_catalog.py \
  --module google-sheets \
  --sources-dir /tmp/roll-google-sheets

# Gates
CATALOG_GATE_MODE=fail uv run pytest server/tests/conformance/test_api_catalog.py \
  -k google-sheets -q

uv run pytest server/tests/conformance/ \
              server/tests/modules/google_sheets/ -q

uv run python scripts/export_connector_registry.py
```

> Note: copy `catalog_sources/` into `/tmp/roll-google-sheets/` before running
> the generator. The generator expects `catalog_sources.json` and
> `official_fields.json` at the root of the sources dir.
