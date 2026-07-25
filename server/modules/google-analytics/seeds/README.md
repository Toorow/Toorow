# GA4 Seed Pipeline

Story 1.4 — local dev/CI loop for the GA4 connector.

---

## Local Fallback Decision

**Chosen tool: DuckDB**

**Rationale:**

| Criterion | DuckDB | BigQuery Emulator |
|---|---|---|
| Infrastructure | Zero (pip-installable) | Requires Docker |
| CI compatibility | Native (no sidecar) | Docker-in-Docker or separate service |
| Dev-loop speed | Fastest (in-process) | Slower (Docker startup) |
| SQL dialect parity | Minor differences vs BQ | Identical |
| Switch to real BigQuery | `profiles.yml` only | `profiles.yml` only |

DuckDB is chosen for P0/Story 1.4 because it requires no infrastructure, matches
the "prove the loop locally" goal (SOLUTION-DESIGN §9), and `dbt-duckdb` is
production-grade (≥1.9.x, compatible with dbt-core 1.11.x).

**BigQuery path is NOT a rewrite — it is a config switch:**

To point the dbt project at real BigQuery instead of local DuckDB, change
`~/.dbt/profiles.yml` (or `DBT_PROFILES_DIR`) to use the `bigquery` target.
No SQL model changes are required.

The `load_seed.py` loader similarly switches via `--mode bigquery --bq-project <id>`.

---

## Raw Table Schema

```
raw_ga4_standard_daily:
  date            VARCHAR   -- ISO-8601 date string (e.g. "2026-04-01")
  device_category VARCHAR   -- "desktop" | "mobile" | "tablet"
  country         VARCHAR   -- GA4 full-name format (e.g. "France", "United Kingdom")
  sessions        INTEGER
  active_users    INTEGER
  conversions     INTEGER
  pull_id         VARCHAR   -- e.g. pull_01J9ZF3KWTQR1F5G2H3X4Y5Z6A  (AD-7)
  loaded_at       VARCHAR   -- UTC ISO-8601 timestamp (e.g. "2026-07-10T12:34:56Z")
```

> **AD-7:** `pull_id` is minted once per loader invocation. All rows in the
> same batch share the same `pull_id`. The table is **append-only** — re-running
> the loader appends a new batch rather than replacing existing rows.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `TOOROW_DB_MODE` | `duckdb` | `duckdb` or `bigquery` |
| `TOOROW_DUCKDB_PATH` | `seeds/local.duckdb` | Path to local DuckDB file |
| `GCP_PROJECT` | _(none)_ | GCP project ID (BigQuery mode only) |
| `GOOGLE_APPLICATION_CREDENTIALS` | _(none)_ | Service-account key path (BigQuery mode only) |

---

## Quick Start

### Prerequisites

| Tool | Version |
|---|---|
| Python | 3.12 |
| uv | latest |
| dbt-core | 1.11.x (installed via `uv sync`) |
| dbt-duckdb | ≥1.9.x (installed via `uv sync`) |
| duckdb | ≥1.0 (installed via `uv sync`) |
| python-ulid | ≥3.0 (installed via `uv sync`) |

### One-shot local loop

```bash
# From repo root — runs generate + load + dbt run + dbt test:
uv run python server/modules/google-analytics/seeds/run_local_loop.py
```

### Step-by-step

```bash
# 1. Generate the seed CSV (90 days of realistic GA4 data)
uv run python server/modules/google-analytics/seeds/generate_seed.py

# 2. Load into local DuckDB (mints a pull_id, appends rows)
uv run python server/modules/google-analytics/seeds/load_seed.py \
    --mode duckdb \
    --duckdb-path server/modules/google-analytics/seeds/local.duckdb

# 3. Run dbt models (staging + mart)
cd dbt
dbt run --select google_analytics --profiles-dir profiles
dbt test --select google_analytics --profiles-dir profiles
```

### Switching to real BigQuery

1. Set up a `~/.dbt/profiles.yml` using the template at `dbt/profiles/profiles.yml.example` 
   and choose the `bigquery` target.
2. Run the loader with BigQuery mode:
   ```bash
   uv run python server/modules/google-analytics/seeds/load_seed.py \
       --mode bigquery \
       --bq-project <your-gcp-project>
   ```
3. Run dbt against BigQuery:
   ```bash
   cd dbt
   dbt run --select google_analytics --target bigquery
   dbt test --select google_analytics --target bigquery
   ```

No SQL model changes are required — only the `profiles.yml` target changes.

---

## Generated Files (gitignored)

| File | Description |
|---|---|
| `ga4_seed.csv` | Generated seed CSV (90 days × 3 devices × 5 countries = 1350 rows) |
| `local.duckdb` | Local DuckDB warehouse file |

These files are listed in `.gitignore` and must never be committed.
