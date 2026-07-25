# dbt Seeds

This directory contains declarative seed CSVs loaded by `dbt seed`.

## Seeds

### `metric_source_priority.csv`
Priority order for cross-source metric dedup (Story 3.7). Governs which connector wins
when multiple connectors report the same canonical metric (e.g. `conversions`).

### `dim_country.csv`, `dim_device.csv`, `dim_metric.csv`
Canonical dimension dictionaries (Story 4.1). Used by the `normalize_dimension` macro
to map raw source values to canonical vocabulary.

### `project_preferences.csv`
**FALLBACK-ONLY — superseded by `mirror.project_preferences` (Story 4.4).**

The canonical source is now `mirror.project_preferences` populated by
`server/core/mirror_sync.py`. Use the mirror path in all normal-operation and
production scenarios.

This CSV seed is retained as a **CI/offline fallback** for environments without Postgres
(when `SYNC_ENABLED=false`). Do NOT rely on it for production. It contains only a
minimal default row and will not reflect real project preferences.

**Normal operation (Postgres + mirror sync available):**
The mirror is populated automatically by the nightly scheduler or on demand:
```bash
uv run python -m core.mirror_sync
# or trigger via admin API:
# POST /api/mirror/sync
```

**CI / offline fallback (SYNC_ENABLED=false, no Postgres):**
dbt falls back to this seed file automatically. The `sources_mirror.yml` source is
conditionally disabled when `SYNC_ENABLED=false` (see `dbt/models/staging/sources_mirror.yml`).

**To regenerate this fallback CSV from Postgres (emergency only):**
```bash
uv run python scripts/export_project_preferences.py  # SUPERSEDED — see warning in script
```

Contains project-level preferences for cost and timezone normalization:
- `canonical_currency`: ISO-4217 code (e.g. EUR) — the normalization target for all spend.
- `reporting_timezone`: IANA timezone name (e.g. Europe/Paris) — date-labeling policy.

### `fx_rates.csv`
Static FX rate seed for dev-time cost normalization (Story 4.2).
Columns: `from_currency`, `to_currency`, `rate`, `rate_date`, `rate_policy`.

Includes:
- USD→EUR: 0.92 (2026-07-01, static_dev_rate)
- EUR→EUR: 1.00 (2026-07-01, identity)

**Production FX feed (live ECB rates or similar) is deferred to Story 4.4 / Epic 6.**
This seed provides a static dev-time rate for testing normalization logic only.
