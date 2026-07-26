"""GA4 seed loader — Story 1.4, T3.2.

Reads the generated ga4_seed.csv, stamps each row with a ``pull_id`` (ULID,
prefixed ``pull_`` per AD-7) and a ``loaded_at`` UTC timestamp, then inserts
rows into either a local DuckDB file or BigQuery (append-only, never overwrite).

# AD-7: pull_id minted at dispatch (core scheduler in Epic 3; seed loader at P0)
# AD-7: pull_id minted once per invocation; append-only; never overwrite

Usage (DuckDB — default local mode):
    python load_seed.py
    python load_seed.py --mode duckdb --csv-path ga4_seed.csv --duckdb-path local.duckdb

Usage (BigQuery — requires GCP credentials):
    python load_seed.py --mode bigquery --bq-project my-gcp-project
"""

from __future__ import annotations

import argparse
import csv
import os
from datetime import datetime, timezone
from pathlib import Path

from ulid import ULID

# ---------------------------------------------------------------------------
# Raw table schema (AD-7: append-only, never overwrite)
#
# raw_ga4_standard_daily:
#   date            VARCHAR   (ISO-8601 date string from seed CSV)
#   device_category VARCHAR
#   country         VARCHAR
#   sessions        INTEGER
#   active_users    INTEGER
#   conversions     INTEGER
#   pull_id         VARCHAR   -- e.g. pull_01J...
#   loaded_at       VARCHAR   (UTC ISO-8601 timestamp)
# ---------------------------------------------------------------------------

_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_ga4_standard_daily (
    date            VARCHAR,
    device_category VARCHAR,
    country         VARCHAR,
    sessions        INTEGER,
    active_users    INTEGER,
    conversions     INTEGER,
    pull_id         VARCHAR,
    loaded_at       VARCHAR, -- ISO-8601 UTC string; lexicographic sort == chronological (F-06)
    project_id      VARCHAR, -- project binding; 'default' for seeded data (AC6 Story 2.7)
    -- Story 39.7: provenance immuable de la zone de reporting, miroir du
    -- _RAW_CREATE_DDL du connecteur. Le seed n'a pas de metadonnees de propriete
    -- GA4, donc la zone est indeterminee et la colonne reste NULL -- fail-closed
    -- vers un TIMEZONE_GAP a la lecture, jamais un 'UTC' silencieux (E39-AD2).
    report_timezone VARCHAR
)
"""

_INSERT_SQL = """
INSERT INTO raw_ga4_standard_daily
    (date, device_category, country, sessions, active_users, conversions,
     pull_id, loaded_at, project_id, report_timezone)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _mint_pull_id() -> str:
    """Mint a single pull_id for this invocation.

    Format: pull_<ULID-string> (AD-7 / Architecture Spine §Consistency).
    """
    return f"pull_{ULID()}"


def load_duckdb(
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    duckdb_path: str,
    project_id: str = "default",
) -> int:
    """Insert *rows* into a DuckDB database file (local fallback).

    Creates the database and table if they do not exist.
    Always appends — never drops or replaces.
    """
    import duckdb  # noqa: PLC0415 — optional local dep

    con = duckdb.connect(duckdb_path)
    con.execute(_CREATE_DDL)
    # Story 2.7 migration: add project_id column to existing tables that lack it.
    # DuckDB 1.5.4 supports ALTER TABLE ... ADD COLUMN IF NOT EXISTS.
    con.execute(
        "ALTER TABLE raw_ga4_standard_daily ADD COLUMN IF NOT EXISTS "
        "project_id VARCHAR DEFAULT 'default'"
    )
    # Story 39.7 : meme garde additive que le connecteur, pour les entrepots
    # seedes avant l'ajout de la colonne.
    con.execute(
        "ALTER TABLE raw_ga4_standard_daily ADD COLUMN IF NOT EXISTS report_timezone VARCHAR"
    )

    values = [
        (
            r["date"],
            r["device_category"],
            r["country"],
            int(r["sessions"]),
            int(r["active_users"]),
            int(r["conversions"]),
            pull_id,
            loaded_at,
            project_id,
            r.get("report_timezone"),
        )
        for r in rows
    ]
    con.executemany(_INSERT_SQL, values)
    con.close()
    return len(values)


def load_bigquery(
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    bq_project: str,
    project_id: str = "default",
) -> int:
    """Insert *rows* into BigQuery (production path).

    Creates the ``raw_ga4.raw_ga4_standard_daily`` table if it does not exist,
    then appends via WRITE_APPEND disposition (AD-7: never overwrites).
    """
    from google.cloud import bigquery  # noqa: PLC0415 — optional prod dep

    client = bigquery.Client(project=bq_project)

    dataset_ref = bigquery.DatasetReference(bq_project, "raw_ga4")
    try:
        client.get_dataset(dataset_ref)
    except Exception:
        client.create_dataset(dataset_ref)

    schema = [
        bigquery.SchemaField("date", "STRING"),
        bigquery.SchemaField("device_category", "STRING"),
        bigquery.SchemaField("country", "STRING"),
        bigquery.SchemaField("sessions", "INTEGER"),
        bigquery.SchemaField("active_users", "INTEGER"),
        bigquery.SchemaField("conversions", "INTEGER"),
        bigquery.SchemaField("pull_id", "STRING"),
        bigquery.SchemaField("loaded_at", "STRING"),
        bigquery.SchemaField("project_id", "STRING"),
    ]
    table_ref = dataset_ref.table("raw_ga4_standard_daily")
    table = bigquery.Table(table_ref, schema=schema)
    client.create_table(table, exists_ok=True)

    bq_rows = [
        {
            "date": r["date"],
            "device_category": r["device_category"],
            "country": r["country"],
            "sessions": int(r["sessions"]),
            "active_users": int(r["active_users"]),
            "conversions": int(r["conversions"]),
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
    job.result()  # wait for completion
    return len(bq_rows)


def load_csv(csv_path: str) -> list[dict]:
    """Read the seed CSV and return rows as list of dicts."""
    with open(csv_path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def run(
    csv_path: str,
    mode: str,
    duckdb_path: str,
    bq_project: str | None,
    _rows_override: list[dict] | None = None,
    project_id: str = "default",
) -> tuple[str, int]:
    """Main load routine — returns (pull_id, row_count).

    Parameters
    ----------
    _rows_override:
        For testing only — pass pre-built rows instead of reading csv_path.
        When provided, csv_path is ignored.
    project_id:
        Project binding for the seed rows (default: 'default').
        Seed data always uses 'default' so it is separated from real pull data (AC6 Story 2.7).
    """
    pull_id = _mint_pull_id()
    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")

    rows = _rows_override if _rows_override is not None else load_csv(csv_path)

    if mode == "duckdb":
        count = load_duckdb(rows, pull_id, loaded_at, duckdb_path, project_id=project_id)
    elif mode == "bigquery":
        if not bq_project:
            raise ValueError("--bq-project is required for BigQuery mode")
        count = load_bigquery(rows, pull_id, loaded_at, bq_project, project_id=project_id)
    else:
        raise ValueError(f"Unknown mode: {mode!r} — expected 'duckdb' or 'bigquery'")

    return pull_id, count


def main() -> None:
    here = Path(__file__).parent
    default_csv = str(here / "ga4_seed.csv")
    default_duckdb = str(here / "local.duckdb")

    # Allow env-var override (documented in seeds/README.md)
    default_mode = os.environ.get("TOOROW_DB_MODE", "duckdb")
    default_duckdb_path = os.environ.get("TOOROW_DUCKDB_PATH", default_duckdb)

    parser = argparse.ArgumentParser(
        description="Load GA4 seed CSV into local or BigQuery warehouse"
    )
    parser.add_argument("--csv-path", default=default_csv, help="Path to ga4_seed.csv")
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

    pull_id, count = run(
        csv_path=args.csv_path,
        mode=args.mode,
        duckdb_path=args.duckdb_path,
        bq_project=args.bq_project,
    )
    print(f"Loaded {count} rows  pull_id={pull_id}")


if __name__ == "__main__":
    main()
