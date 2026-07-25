# GitHub catalog rollout notes — Story 25.7, updated Epic 31.4

## Catalog counts

| Category               | Count |
|------------------------|-------|
| official_fields total  | 25    |
| currently exposed      | 1 (`date`)  |
| planned (not yet extracted) | 24 |
| enrichment sources     | 0 (MINIMAL-CATALOG — no Supermetrics connector for GitHub) |
| sections               | 4 (TIME, RELEASE, DEPLOYMENT, REPOSITORY) |
| drift_ids expected     | 0 — `date` maps to `published_at/created_at` (compound source_field in manifest matches split per-profile logic in connector.py) |

## Key decisions

- **MINIMAL-CATALOG**: GitHub is a context module (module_kind=context). No Supermetrics enrichment exists. The catalog is honest about its small scope.
- **date field**: The manifest declares `source_field: "published_at/created_at"` (a compound string) because the connector uses `published_at` for releases and `created_at` for deployments at runtime. The official_fields.json entry mirrors this with `source_field: "published_at"` representing the primary releases path; the deployments path is covered by `deployment_created_at`.
- **403 nuance**: GitHub sends 403 for both rate-limit-exceeded (X-RateLimit-Remaining: 0 headers) and scope/permission denied. Current connector raises raw `PermissionError` on 403 without routing through `classify_http_error`. The `_error_map_note` in manifest.json documents this deferred split — Story 25.7 does not restructure the raise sites.
- **account_topology**: Not declared. Token + connection_config(owner+repo) = single repository scope. No discovery call exists.
- **repo_owner / repo_name**: Included in official_fields.json as `_connection_config.*` source fields. They scope every context_event row but come from Nango metadata, not the API payload. Marked `planned` in the generated catalog.

## Orchestrator command block

```bash
# Step 1: prepare sources dir
mkdir -p /tmp/roll-github

# Step 2: copy catalog inputs
cp server/modules/github/catalog_sources/catalog_sources.json /tmp/roll-github/
cp server/modules/github/catalog_sources/official_fields.json /tmp/roll-github/

# Step 3: run the catalog generator
uv run python scripts/build_api_catalog.py \
  --module github \
  --sources-dir /tmp/roll-github \
  --report /tmp/roll-github/report.json

# Step 4: review the fusion report — drift_ids MUST be empty
cat /tmp/roll-github/report.json | python -m json.tool | grep -A5 '"drift_ids"'

# Step 5: if drift_ids is empty, copy the generated catalog into the module
cp /tmp/roll-github/api_catalog.json server/modules/github/api_catalog.json

# Step 6: run conformance + module tests
CATALOG_GATE_MODE=fail uv run pytest server/tests/conformance/test_api_catalog.py -q -k github
uv run pytest server/modules/github/tests/ -q

# Step 7: regenerate public registry
uv run python scripts/export_connector_registry.py
```

## Epic 31.4 landing migration (non-regression)

Applied 2026-07-21. Zero functional regression -- same events emitted as before.

### What changed

- **manifest.json**: `landing: "context_events"` now declared **explicitly** on both
  `releases` and `deployments` profiles (in both `report_profiles[]` and
  `source_capabilities.reports[]`). Previously the landing was derived implicitly
  from `module_kind: "context"`. Behaviour is identical; the declaration is now
  authoritative rather than inferred.
- **manifest.json**: `canonical_event_mapping` added:
  `{"release": "release", "deployment": "deployment"}`. GitHub event ids match the
  canonical dim_event_type.csv types directly (no remapping needed).
- **connector.py**: `_write_context_event()` now delegates to
  `core.context_events.persist_context_event()` (canonical path) instead of
  inserting directly. This adds:
  - `platform="github"` (level 1 of the platform>type>label identity, Epic 31 §2bis)
  - `source="github"` (distinguishes connector-emitted from manual events)
  - `value=None` (pulse -- no MMM magnitude for releases/deployments yet)
  - `validate_event_type()` enforcement (release/deployment are in dim_event_type.csv)
- **module_kind: "context"** retained as legacy hint (soft-deprecation, Epic 31 §11).

### Step 3 (kind:"event" fields) -- SKIPPED

Declaring `published_at`/`deployment_created_at` as `kind:"event"` fields in
`official_fields.json` requires adding `"event"` to the `kind` enum in
`source-capabilities.schema.json` and `api-catalog.schema.json` (Epic 31.1 scope).
Doing this before 31.1 lands would cause schema conformance failures. Deferred to
after 31.1 merges. The connector works correctly without these declarations -- the
event landing is driven by `landing:"context_events"` on the profile, not by field
`kind` declarations.

### Overlay readiness (31.5)

GitHub `release` and `deployment` events are now stamped with `platform="github"` and
canonical types from `dim_event_type.csv` (category `engineering`, marker `diamond`).
They coexist in `app.context_events` with YouTube `video_upload` events
(category `content`, marker `triangle`) and can be superimposed on any metric graph,
filtered by `platform` or `category`.

## No supermetrics file

This module has no enrichment source. Do NOT pass a `--supermetrics` flag.
The `catalog_sources.json` contains exactly one source entry (`kind: "official"`).
