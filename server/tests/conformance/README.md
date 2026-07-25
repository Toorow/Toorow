# Module Conformance Suite

Story 1.8 — core-provided pytest gate every module must pass in CI.

## Invocation

Run the full suite against a module folder:

```sh
uv run pytest server/tests/conformance/ --module-path server/modules/google-analytics/ -v
```

Run only a specific layer:

```sh
uv run pytest server/tests/conformance/ --module-path server/modules/google-analytics/ -m conformance_layer_2
```

## Four-Layer Architecture

Layers execute in order. Layer 1 failure triggers fail-fast (layers 2–4 skip).

| # | Marker | File | What it checks |
|---|--------|------|----------------|
| 1 | `conformance_layer_1` | `test_manifest.py` | `manifest.json` validates against `server/core/schemas/manifest.schema.json` |
| 2 | `conformance_layer_2` | `test_envelope.py` | Every MCP tool returns the canonical AD-1 envelope; summary ≤30 lines |
| 3 | `conformance_layer_3` | `test_bundle.py` | Widget artifact exists and contains no external `http(s)://` references (AD-11 / NFR4) |
| 4 | `conformance_layer_4` | `test_golden_pull.py` | `transform(golden_pull.json)` matches `expected_facts.json` field-by-field |

## Module Fixture Contract

Every module **must** ship these fixture files (mandatory from Story 1.8 onward):

```
<module_path>/
  tests/
    fixtures/
      golden_pull.json      # raw input rows (list[dict]) as they land from the source API
      expected_facts.json   # canonical fact rows after transform() (list[dict])
```

### golden_pull.json row shape (GA4 example)

```json
[
  {
    "pull_id": "pull_test_001",
    "connector": "google-analytics",
    "date": "2026-07-01",
    "deviceCategory": "desktop",
    "country": "France",
    "activeUsers": 1200,
    "sessions": 1450,
    "conversions": 32
  }
]
```

### expected_facts.json row shape

```json
[
  {
    "pull_id": "pull_test_001",
    "connector": "google-analytics",
    "date": "2026-07-01",
    "device_category": "desktop",
    "country": "France",
    "active_users": 1200,
    "sessions": 1450,
    "conversions": 32
  }
]
```

Fields renamed via `canonical_metric_mapping` and `canonical_dimension_mapping` in `manifest.json`.
Pass-through fields (`pull_id`, `connector`, `date`, unmapped dimensions) remain unchanged.

## Error Message Format

Every conformance failure follows:

```
[<layer_name>] <location>: <what was expected> vs <what was found>
```

Examples:
- `[manifest] $.name: must match pattern ^[a-z0-9-]+$, got "BrokenModule"`
- `[envelope] tool=get_ga4_report path=$.meta.alerts: expected array, got null`
- `[bundle] external URL found: https://fonts.googleapis.com/css2?family=Roboto — violates AD-11/NFR4`
- `[golden_pull] row 0 field active_users: expected 1200, got 1000`

## Adding a New Module

1. Add `manifest.json` and `connector.py` under `server/modules/<module-name>/`.
2. Add `transform(raw_rows: list[dict]) -> list[dict]` to `connector.py` (manifest-driven, no hardcoded source field names).
3. Create `server/modules/<module-name>/tests/fixtures/golden_pull.json` and `expected_facts.json`.
4. Run `uv run pytest server/tests/conformance/ --module-path server/modules/<module-name>/ -v`.
5. All four layers must pass before the module is considered conformant.

## Broken-Module Fixture

`server/tests/conformance/fixtures/broken-module/` is a test-only fixture that exercises
every layer's error detection. It is **never** placed under `server/modules/` and is
never loaded by the real module loader.

| Layer | Violation | Expected error substring |
|-------|-----------|--------------------------|
| 1 | `name: "BrokenModule"` (uppercase, fails `^[a-z0-9-]+$`) | `name` |
| 1 | `auth_type: "unknown"` (not in enum) | `auth_type` |
| 2 | tool returns `{"result": []}` (no `schema_version`, `meta`, `data`) | `schema_version` |
| 4 | transform() subtracts 1 from `active_users`; wrong expected detected | `active_users` |
