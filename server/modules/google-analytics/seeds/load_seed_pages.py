"""GA4 pages_daily seed loader -- Epic 10, story 10.3.

Loads the generated pages seeds into the local DuckDB (or BigQuery) raw tables,
stamping each row with a ``pull_id`` (ULID, prefixed ``pull_`` per AD-7) and a
``loaded_at`` UTC timestamp. Append-only, never overwrite.

TWO profiles (Task 1 design decision -- landingPage and pagePath are distinct GA4
dimensions with NO join, so two independent top-N lists):
  * --profile landing -> ga4_landing_seed.csv -> raw_ga4_landing_daily
    (columns: date, landing_page, sessions)
  * --profile paths   -> ga4_paths_seed.csv   -> raw_ga4_paths_daily
    (columns: date, page, screen_page_views)

Parallel, non-interfering with load_seed.py / load_seed_user_type.py.

# AD-7: pull_id minted once per invocation; append-only; never overwrite.

Usage (DuckDB -- default local mode):
    python load_seed_pages.py --profile landing
    python load_seed_pages.py --profile paths --duckdb-path local.duckdb

Usage (BigQuery -- requires GCP credentials):
    python load_seed_pages.py --profile landing --mode bigquery --bq-project my-gcp-project
"""

from __future__ import annotations

import argparse
import csv
import os
from datetime import datetime, timezone
from pathlib import Path

from ulid import ULID

# ---------------------------------------------------------------------------
# Per-profile config: raw table, DDL, insert SQL, seed value column.
# AD-7: append-only, never overwrite.
# ---------------------------------------------------------------------------

_CREATE_DDL_LANDING = """
CREATE TABLE IF NOT EXISTS raw_ga4_landing_daily (
    date         VARCHAR,
    landing_page VARCHAR,
    sessions     INTEGER,
    pull_id      VARCHAR,
    loaded_at    VARCHAR,
    project_id   VARCHAR
)
"""

_INSERT_SQL_LANDING = """
INSERT INTO raw_ga4_landing_daily
    (date, landing_page, sessions, pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?)
"""

_CREATE_DDL_PATHS = """
CREATE TABLE IF NOT EXISTS raw_ga4_paths_daily (
    date              VARCHAR,
    page              VARCHAR,
    screen_page_views INTEGER,
    pull_id           VARCHAR,
    loaded_at         VARCHAR,
    project_id        VARCHAR
)
"""

_INSERT_SQL_PATHS = """
INSERT INTO raw_ga4_paths_daily
    (date, page, screen_page_views, pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?)
"""

# profile -> (raw_table, dim_column, value_column, create_ddl, insert_sql, bq_value_type)
_PROFILES = {
    "landing": (
        "raw_ga4_landing_daily",
        "landing_page",
        "sessions",
        _CREATE_DDL_LANDING,
        _INSERT_SQL_LANDING,
    ),
    "paths": (
        "raw_ga4_paths_daily",
        "page",
        "screen_page_views",
        _CREATE_DDL_PATHS,
        _INSERT_SQL_PATHS,
    ),
}


def _mint_pull_id() -> str:
    """Mint a single pull_id for this invocation. Format: pull_<ULID-string> (AD-7)."""
    return f"pull_{ULID()}"


def load_pages_duckdb(
    profile: str,
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    duckdb_path: str,
    project_id: str = "default",
) -> int:
    """Insert *rows* into the profile's raw table in a DuckDB file (local fallback).

    Creates the database and table if they do not exist. Always appends (AD-7).
    """
    import duckdb  # noqa: PLC0415 -- optional local dep

    raw_table, dim_col, value_col, create_ddl, insert_sql = _PROFILES[profile]

    con = duckdb.connect(duckdb_path)
    con.execute(create_ddl)
    con.execute(
        f"ALTER TABLE {raw_table} ADD COLUMN IF NOT EXISTS "
        "project_id VARCHAR DEFAULT 'default'"
    )
    values = [
        (
            r["date"],
            r[dim_col],
            int(r[value_col]),
            pull_id,
            loaded_at,
            project_id,
        )
        for r in rows
    ]
    con.executemany(insert_sql, values)
    con.close()
    return len(values)


def load_pages_bigquery(
    profile: str,
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    bq_project: str,
    project_id: str = "default",
) -> int:
    """Insert *rows* into raw_ga4.<raw_table> in BigQuery (WRITE_APPEND)."""
    from google.cloud import bigquery  # noqa: PLC0415 -- optional prod dep

    raw_table, dim_col, value_col, _create_ddl, _insert_sql = _PROFILES[profile]

    client = bigquery.Client(project=bq_project)
    dataset_ref = bigquery.DatasetReference(bq_project, "raw_ga4")
    try:
        client.get_dataset(dataset_ref)
    except Exception:
        client.create_dataset(dataset_ref)

    schema = [
        bigquery.SchemaField("date", "STRING"),
        bigquery.SchemaField(dim_col, "STRING"),
        bigquery.SchemaField(value_col, "INTEGER"),
        bigquery.SchemaField("pull_id", "STRING"),
        bigquery.SchemaField("loaded_at", "STRING"),
        bigquery.SchemaField("project_id", "STRING"),
    ]
    table_ref = dataset_ref.table(raw_table)
    table = bigquery.Table(table_ref, schema=schema)
    client.create_table(table, exists_ok=True)

    bq_rows = [
        {
            "date": r["date"],
            dim_col: r[dim_col],
            value_col: int(r[value_col]),
            "pull_id": pull_id,
            "loaded_at": loaded_at,
            "project_id": project_id,
        }
        for r in rows
    ]

    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
    )
    job = client.load_table_from_json(bq_rows, table_ref, job_config=job_config)
    job.result()
    return len(bq_rows)


def load_csv(csv_path: str) -> list[dict]:
    """Read a pages seed CSV and return rows as a list of dicts."""
    with open(csv_path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def run(
    profile: str,
    csv_path: str,
    mode: str,
    duckdb_path: str,
    bq_project: str | None,
    _rows_override: list[dict] | None = None,
    project_id: str = "default",
) -> tuple[str, int]:
    """Main load routine -- returns (pull_id, row_count).

    _rows_override: for testing only -- pass pre-built rows instead of reading csv_path.
    """
    if profile not in _PROFILES:
        raise ValueError(f"Unknown profile: {profile!r} -- expected 'landing' or 'paths'")

    pull_id = _mint_pull_id()
    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")

    rows = _rows_override if _rows_override is not None else load_csv(csv_path)

    if mode == "duckdb":
        count = load_pages_duckdb(
            profile, rows, pull_id, loaded_at, duckdb_path, project_id=project_id
        )
    elif mode == "bigquery":
        if not bq_project:
            raise ValueError("--bq-project is required for BigQuery mode")
        count = load_pages_bigquery(
            profile, rows, pull_id, loaded_at, bq_project, project_id=project_id
        )
    else:
        raise ValueError(f"Unknown mode: {mode!r} -- expected 'duckdb' or 'bigquery'")

    return pull_id, count


def main() -> None:
    here = Path(__file__).parent
    default_duckdb = str(here / "local.duckdb")
    default_mode = os.environ.get("TOOROW_DB_MODE", "duckdb")
    default_duckdb_path = os.environ.get("TOOROW_DUCKDB_PATH", default_duckdb)

    parser = argparse.ArgumentParser(
        description="Load GA4 pages_daily seed CSV into local or BigQuery warehouse"
    )
    parser.add_argument(
        "--profile",
        choices=["landing", "paths"],
        required=True,
        help="Which pages profile to load (landing = entry pages; paths = top pages)",
    )
    parser.add_argument(
        "--csv-path",
        default=None,
        help="Path to the seed CSV (defaults per profile next to this file)",
    )
    parser.add_argument(
        "--mode",
        choices=["duckdb", "bigquery"],
        default=default_mode,
        help="Storage backend (default: duckdb)",
    )
    parser.add_argument(
        "--duckdb-path",
        default=default_duckdb_path,
        help="DuckDB file path (duckdb mode only)",
    )
    parser.add_argument("--bq-project", default=None, help="GCP project ID (bigquery mode only)")
    args = parser.parse_args()

    default_csv = {
        "landing": str(here / "ga4_landing_seed.csv"),
        "paths": str(here / "ga4_paths_seed.csv"),
    }[args.profile]

    pull_id, count = run(
        profile=args.profile,
        csv_path=args.csv_path or default_csv,
        mode=args.mode,
        duckdb_path=args.duckdb_path,
        bq_project=args.bq_project,
    )
    print(f"Loaded {count} {args.profile} page rows  pull_id={pull_id}")


if __name__ == "__main__":
    main()
