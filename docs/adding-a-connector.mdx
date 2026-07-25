---
title: Add a connector
description: "toorow's extensibility contract: manifest, connector.py, dbt staging, 4-layer conformance, and Mailgun step-by-step walkthrough."
---

> **Canonical reference**: This guide describes the extensibility contract as it is *actually* implemented in the repository. Read it **in full** before creating a single file.

---

## Overview

### What You Get for Free (The Shared Base)

Any module that respects the contract inherits the following mechanisms without writing a single line in `server/core/`:

| Mechanism | Core file | What you get |
|---|---|---|
| **Journaling** | `core/datastreams.py`, `core/audit.py` | Append-only ULID `pull_id` (AD-7), `audit_log` rows, daily ledger (ok/partial/failed/missing statuses). |
| **Queue / Retry / Quota** | `core/queue.py`, `core/quota.py` | Worker with retry, dead-letter, deduplication, per-provider circuit-breaker; `RateLimitError` automatically handled. |
| **Dictionary / Mappings** | `app.target_fields`, `app.datastream_mappings` | Auto-seeded from the manifest's `canonical_metric_mapping` + `canonical_dimension_mapping` on the first `backfill_datastreams()`. |
| **Quality Monitors** | `core/dq_monitors.py` | 5 universal monitors (volume, timeliness, duplication, schema drift, date format). |
| **MCP Flows** | `core/flows.py` | `flows_list/get/upsert/validate` — the agent and the admin UI edit your datastreams through the exact same interface. |
| **Admin Screens** | `ui/` | Overview, Data model (used-by), extraction calendar, source→target mapping, Quality page. |

---

### What You Must Write (The Cost of a New Connector)

```
server/modules/<kebab-name>/
  manifest.json                      # Required (schema version 1.2)
  connector.py                       # Required (pull + transform functions)
  dbt/staging/stg_<name>_daily.sql   # Recommended (QUALIFY supersede AD-7)
  dbt/staging/schema.yml             # Recommended (grain_unique test)
  reports/<id>.json                  # ≥ 1 report pack
  tests/fixtures/golden_pull.json    # Required (Layer 4 conformance)
  tests/fixtures/expected_facts.json # Required (Layer 4 conformance)
```

No file inside `server/core/` may be modified (AD-2). Add the module's staging path to `dbt/dbt_project.yml` under `model-paths`.

---

## Choosing the Landing Kind

The `module_kind` field in `manifest.json` declares the structure of rows produced by the module:

| Kind | Row Structure | Typical Examples |
|---|---|---|
| `kpi` | `(project_id, date, metric, value, breakdown_dimension, breakdown_value, pull_id, loaded_at)` — long format | GA4, Meta Ads, TikTok, LinkedIn, Shopify, Stripe, Mailgun delivery stats. |
| `context` | `(project_id, date, event_type, event_payload)` — point-in-time events | GitHub releases, CI/CD deployments, CRM milestones, campaign launches. |
| `generic` | Arbitrary tabular data mapped via `datastream_mappings` | CSV exports, generic webhooks. |

---

## Step-by-Step Walkthrough: Building a Mailgun Connector

This section demonstrates initializing and implementing a new connector module step-by-step using **Mailgun** (`server/modules/mailgun/`) as a concrete example.

### Step 1 — Scaffolding the Directory Structure

Create the target directory `server/modules/mailgun/`:

```
server/modules/mailgun/
  manifest.json
  connector.py
  dbt/staging/
    stg_mailgun_daily.sql
    schema.yml
  reports/
    daily_delivery.json
  tests/fixtures/
    golden_pull.json
    expected_facts.json
```

---

### Step 2 — Manifest Declaration (`server/modules/mailgun/manifest.json`)

Declare schema version `"1.2"`, metadata, metric mappings, and `source_capabilities`:

```json
{
  "schema_version": "1.2",
  "name": "mailgun",
  "display_name": "Mailgun Email Analytics",
  "auth_type": "api_key",
  "module_kind": "kpi",
  "report_profiles": [
    {
      "id": "daily",
      "display_name": "Daily Email Delivery",
      "metrics": ["accepted", "delivered", "opened", "clicked", "failed"],
      "dimensions": ["date", "domain"],
      "extraction_capabilities": {
        "row_limit": 25000,
        "filters_supported": true,
        "realtime": false
      },
      "extraction_path": "custom_pull"
    }
  ],
  "canonical_metric_mapping": {
    "accepted": "accepted_events",
    "delivered": "delivered_events",
    "opened": "open_events",
    "clicked": "click_events",
    "failed": "failed_events"
  },
  "canonical_dimension_mapping": {
    "domain": "domain"
  },
  "source_capabilities": {
    "contract_version": "1",
    "field_discovery": { "mode": "static", "allowed_targets": [] },
    "fields": [
      { "field_id": "accepted", "source_field": "accepted", "kind": "metric", "physical_type": "integer", "description": "Accepted email count.", "semantic_hints": ["email"], "canonical_target": "accepted_events", "aggregation": "sum", "non_additive": false },
      { "field_id": "delivered", "source_field": "delivered", "kind": "metric", "physical_type": "integer", "description": "Delivered email count.", "semantic_hints": ["email"], "canonical_target": "delivered_events", "aggregation": "sum", "non_additive": false },
      { "field_id": "opened", "source_field": "opened", "kind": "metric", "physical_type": "integer", "description": "Opened email count.", "semantic_hints": ["email"], "canonical_target": "open_events", "aggregation": "sum", "non_additive": false },
      { "field_id": "clicked", "source_field": "clicked", "kind": "metric", "physical_type": "integer", "description": "Clicked link count.", "semantic_hints": ["email"], "canonical_target": "click_events", "aggregation": "sum", "non_additive": false },
      { "field_id": "failed", "source_field": "failed", "kind": "metric", "physical_type": "integer", "description": "Failed delivery count.", "semantic_hints": ["email"], "canonical_target": "failed_events", "aggregation": "sum", "non_additive": false },
      { "field_id": "date", "source_field": "time", "kind": "dimension", "physical_type": "date", "description": "Reporting date.", "semantic_hints": ["date"], "canonical_target": "date", "aggregation": "none", "non_additive": false },
      { "field_id": "domain", "source_field": "domain", "kind": "dimension", "physical_type": "string", "description": "Sending domain.", "semantic_hints": ["domain"], "canonical_target": "domain", "aggregation": "none", "non_additive": false }
    ],
    "reports": [{
      "id": "daily",
      "selection_mode": "exact_bundle",
      "availability": { "status": "selectable" },
      "dispatch": { "callable": "pull" },
      "metrics": ["accepted", "delivered", "opened", "clicked", "failed"],
      "dimensions": ["date", "domain"],
      "supported_grains": [["date", "domain"]],
      "compatibility": [],
      "filters": [{ "field_id": "date", "operators": ["gte", "lte"] }],
      "pagination": { "mode": "none", "completeness": "hard_limit", "max_pages": null, "row_limit": 25000, "truncation_signal": "explicit" },
      "quota_cost": { "read_points": 1, "unit": "request" },
      "incremental": { "mode": "date_window", "cursor_field": null },
      "cadence": { "minimum_interval_minutes": 1440, "supported_modes": ["manual", "daily"] }
    }]
  },
  "quota": {
    "window_seconds": 60,
    "budget_points": 300,
    "read_cost": 1,
    "write_cost": 1
  }
}
```

---

### Step 3 — Implementation (`server/modules/mailgun/connector.py`)

Implement `pull()` to fetch Mailgun Total Stats API (`/v3/{domain}/stats/total`) and manifest-driven `transform()`:

```python
"""Mailgun Email Analytics connector.

AD-2: module name never hardcoded in core/.
AD-3: API key used immediately, never persisted or logged.
AD-7: pull_id minted by the core scheduler, passed in.
"""

from __future__ import annotations
import json
import logging
import os
from pathlib import Path
import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)
mcp_app = FastMCP("mailgun")

def _insert_raw_rows(rows: list[dict], pull_id: str, project_id: str) -> int:
    import duckdb
    duckdb_path = os.environ.get("TOOROW_DUCKDB_PATH", "local.duckdb")
    con = duckdb.connect(duckdb_path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS raw_mailgun_daily (
            date VARCHAR, metric VARCHAR, value DOUBLE,
            breakdown_dimension VARCHAR, breakdown_value VARCHAR,
            pull_id VARCHAR, loaded_at VARCHAR, project_id VARCHAR
        )
    """)
    values = [
        (
            r.get("date", ""),
            r.get("metric", ""),
            float(r.get("value", 0.0) or 0.0),
            r.get("breakdown_dimension", "domain"),
            r.get("breakdown_value", r.get("domain", "")),
            pull_id,
            "2026-07-25T00:00:00Z",
            project_id
        )
        for r in rows
    ]
    if values:
        con.executemany("INSERT INTO raw_mailgun_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?)", values)
    con.close()
    return len(values)

def pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
) -> dict:
    """Fetch Mailgun stats and land raw rows."""
    from core import nango_client  # AD-2

    api_key = nango_client.get_fresh_token(connection_id, provider="mailgun")
    domain = os.environ.get("MAILGUN_DOMAIN", "mg.example.com")

    resp = httpx.get(
        f"https://api.mailgun.net/v3/{domain}/stats/total",
        auth=("api", api_key),
        params={"event": ["accepted", "delivered", "opened", "clicked", "failed"], "start": date_from, "end": date_to},
        timeout=30.0
    )

    if resp.status_code == 429:
        from core.quota import RateLimitError
        raise RateLimitError("mailgun", retry_after=60)
    if resp.status_code != 200:
        raise RuntimeError(f"Mailgun API error {resp.status_code}: {resp.text}")

    raw_items = resp.json().get("stats") or []
    canonical_rows = transform(raw_items)
    row_count = _insert_raw_rows(canonical_rows, pull_id, project_id)

    return {"pull_id": pull_id, "row_count": row_count, "date_from": date_from, "date_to": date_to}

def transform(raw_rows: list[dict]) -> list[dict]:
    """Map Mailgun API response fields to canonical target metrics."""
    manifest = json.loads((Path(__file__).parent / "manifest.json").read_text(encoding="utf-8"))
    metric_map = manifest.get("canonical_metric_mapping", {})

    canonical = []
    for item in raw_rows:
        dt = item.get("time", "")[:10]
        for src_metric, canonical_name in metric_map.items():
            if src_metric in item:
                val = item[src_metric].get("total", 0) if isinstance(item[src_metric], dict) else item[src_metric]
                canonical.append({
                    "date": dt,
                    "metric": canonical_name,
                    "value": val,
                    "breakdown_dimension": "domain",
                    "breakdown_value": item.get("domain", "default")
                })
    return canonical
```

---

### Step 4 — Staging dbt Layer (`stg_mailgun_daily.sql` & `schema.yml`)

Create `server/modules/mailgun/dbt/staging/stg_mailgun_daily.sql`:

```sql
-- QUALIFY supersede (AD-7): latest pull per grain wins
{{ config(materialized='view') }}

SELECT
    date,
    metric,
    value,
    breakdown_dimension,
    breakdown_value,
    pull_id,
    loaded_at,
    project_id
FROM {{ source('raw_mailgun', 'raw_mailgun_daily') }}
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY project_id, date, metric, breakdown_dimension, breakdown_value
    ORDER BY pull_id DESC
) = 1
```

Create `server/modules/mailgun/dbt/staging/schema.yml` with mandatory `grain_unique` test:

```yaml
version: 2
sources:
  - name: raw_mailgun
    schema: main
    tables:
      - name: raw_mailgun_daily
models:
  - name: stg_mailgun_daily
    columns:
      - name: date
        tests: [not_null]
      - name: metric
        tests: [not_null]
      - name: pull_id
        tests: [not_null]
    tests:
      - unique:
          name: stg_mailgun_daily_grain_unique
          arguments:
            column_name: "project_id || '|' || date || '|' || metric || '|' || breakdown_dimension || '|' || breakdown_value"
```

---

### Step 5 — Test Fixtures & 4-Layer Conformance Suite

Populate fixture files in `server/modules/mailgun/tests/fixtures/`:

- `golden_pull.json`: Raw Mailgun API JSON payload.
- `expected_facts.json`: Expected output of `transform(golden_pull.json)`.

Execute the repository conformance suite:

```bash
uv run pytest server/tests/conformance/ --module-path server/modules/mailgun/ -v
```

---

## Final Conformance Checklist

- [ ] `manifest.json` schema version `1.2` valid (`conformance_layer_1` pass).
- [ ] `connector.py` implements `pull()` and manifest-driven `transform()` (no hardcoded field names).
- [ ] `QUALIFY` supersede staging model created with mandatory `stg_mailgun_daily_grain_unique` test.
- [ ] Fixture files `golden_pull.json` and `expected_facts.json` verified (`conformance_layer_4` pass).
- [ ] All 4 conformance layers pass cleanly before merging.

---

## Next Steps & Cross-References

<CardGroup cols={2}>
  <Card title="Universal Datastreams" icon="database" href="/universal-datastreams">
    Learn how datastreams manage ingestion, mappings, and versioning.
  </Card>
  <Card title="Semantic Layer" icon="layer-group" href="/semantic-layer">
    Understand canonical fact surfaces and dbt semantic ratio views.
  </Card>
  <Card title="Platform Constraints" icon="shield-check" href="/constraints">
    Review multi-tenant isolation rules and security invariants.
  </Card>
  <Card title="Quickstart Guide" icon="rocket" href="/quickstart">
    Test your new connector in the local developer sandbox.
  </Card>
</CardGroup>
