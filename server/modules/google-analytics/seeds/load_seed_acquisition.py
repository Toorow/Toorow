"""GA4 acquisition seed loader -- Epic 16, story 16.1.

Loads the generated acquisition seeds into the local DuckDB (or BigQuery) raw tables,
stamping each row with a ``pull_id`` (ULID, prefixed ``pull_`` per AD-7) and a
``loaded_at`` UTC timestamp. Append-only, never overwrite.

TWO profiles (Task 1 design decision -- last click and first click are distinct GA4
runReports with NO cross-join, so two independent top-N lists):
  * --profile session    -> ga4_acquisition_session_seed.csv -> raw_ga4_acquisition_session
    (columns: date, session_source_medium, session_campaign, conversions, sessions)
  * --profile first_user -> ga4_acquisition_first_user_seed.csv
    -> raw_ga4_acquisition_first_user
    (columns: date, first_user_source_medium, conversions)

Parallel, non-interfering with the other GA4 seed loaders.

# AD-7: pull_id minted once per invocation; append-only; never overwrite.

Usage (DuckDB -- default local mode):
    python load_seed_acquisition.py --profile session
    python load_seed_acquisition.py --profile first_user --duckdb-path local.duckdb

Usage (BigQuery -- requires GCP credentials):
    python load_seed_acquisition.py --profile session --mode bigquery --bq-project my-gcp-project
"""

from __future__ import annotations

import argparse
import csv
import os
from datetime import datetime, timezone
from pathlib import Path

from ulid import ULID

# ---------------------------------------------------------------------------
# Per-profile config: raw table, DDL, insert SQL, dim + value columns.
# AD-7: append-only, never overwrite.
# ---------------------------------------------------------------------------

_CREATE_DDL_SESSION = """
CREATE TABLE IF NOT EXISTS raw_ga4_acquisition_session (
    date                  VARCHAR,
    session_source_medium VARCHAR,
    session_campaign      VARCHAR,
    conversions           INTEGER,
    sessions              INTEGER,
    pull_id               VARCHAR,
    loaded_at             VARCHAR,
    project_id            VARCHAR
)
"""

_INSERT_SQL_SESSION = """
INSERT INTO raw_ga4_acquisition_session
    (date, session_source_medium, session_campaign, conversions, sessions,
     pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

_CREATE_DDL_FIRST_USER = """
CREATE TABLE IF NOT EXISTS raw_ga4_acquisition_first_user (
    date                     VARCHAR,
    first_user_source_medium VARCHAR,
    conversions              INTEGER,
    pull_id                  VARCHAR,
    loaded_at                VARCHAR,
    project_id               VARCHAR
)
"""

_INSERT_SQL_FIRST_USER = """
INSERT INTO raw_ga4_acquisition_first_user
    (date, first_user_source_medium, conversions,
     pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?)
"""

# profile -> (raw_table, create_ddl, insert_sql, value_builder)
# value_builder maps a CSV row dict -> the positional tuple (without pull_id/loaded_at/
# project_id, which are appended by the loader).
_PROFILES = {
    "session": {
        "raw_table": "raw_ga4_acquisition_session",
        "create_ddl": _CREATE_DDL_SESSION,
        "insert_sql": _INSERT_SQL_SESSION,
        "row_values": lambda r: (
            r["date"],
            r["session_source_medium"],
            r["session_campaign"],
            int(r["conversions"]),
            int(r["sessions"]),
        ),
        "bq_schema": [
            ("date", "STRING"),
            ("session_source_medium", "STRING"),
            ("session_campaign", "STRING"),
            ("conversions", "INTEGER"),
            ("sessions", "INTEGER"),
        ],
        "bq_row": lambda r: {
            "date": r["date"],
            "session_source_medium": r["session_source_medium"],
            "session_campaign": r["session_campaign"],
            "conversions": int(r["conversions"]),
            "sessions": int(r["sessions"]),
        },
    },
    "first_user": {
        "raw_table": "raw_ga4_acquisition_first_user",
        "create_ddl": _CREATE_DDL_FIRST_USER,
        "insert_sql": _INSERT_SQL_FIRST_USER,
        "row_values": lambda r: (
            r["date"],
            r["first_user_source_medium"],
            int(r["conversions"]),
        ),
        "bq_schema": [
            ("date", "STRING"),
            ("first_user_source_medium", "STRING"),
            ("conversions", "INTEGER"),
        ],
        "bq_row": lambda r: {
            "date": r["date"],
            "first_user_source_medium": r["first_user_source_medium"],
            "conversions": int(r["conversions"]),
        },
    },
}


def _mint_pull_id() -> str:
    """Mint a single pull_id for this invocation. Format: pull_<ULID-string> (AD-7)."""
    return f"pull_{ULID()}"


def load_acquisition_duckdb(
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

    cfg = _PROFILES[profile]
    con = duckdb.connect(duckdb_path)
    con.execute(cfg["create_ddl"])
    con.execute(
        f"ALTER TABLE {cfg['raw_table']} ADD COLUMN IF NOT EXISTS "
        "project_id VARCHAR DEFAULT 'default'"
    )
    values = [cfg["row_values"](r) + (pull_id, loaded_at, project_id) for r in rows]
    con.executemany(cfg["insert_sql"], values)
    con.close()
    return len(values)


def load_acquisition_bigquery(
    profile: str,
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    bq_project: str,
    project_id: str = "default",
) -> int:
    """Insert *rows* into raw_ga4.<raw_table> in BigQuery (WRITE_APPEND)."""
    from google.cloud import bigquery  # noqa: PLC0415 -- optional prod dep

    cfg = _PROFILES[profile]
    client = bigquery.Client(project=bq_project)
    dataset_ref = bigquery.DatasetReference(bq_project, "raw_ga4")
    try:
        client.get_dataset(dataset_ref)
    except Exception:
        client.create_dataset(dataset_ref)

    schema = [bigquery.SchemaField(n, t) for n, t in cfg["bq_schema"]]
    schema += [
        bigquery.SchemaField("pull_id", "STRING"),
        bigquery.SchemaField("loaded_at", "STRING"),
        bigquery.SchemaField("project_id", "STRING"),
    ]
    table_ref = dataset_ref.table(cfg["raw_table"])
    table = bigquery.Table(table_ref, schema=schema)
    client.create_table(table, exists_ok=True)

    bq_rows = []
    for r in rows:
        row = cfg["bq_row"](r)
        row["pull_id"] = pull_id
        row["loaded_at"] = loaded_at
        row["project_id"] = project_id
        bq_rows.append(row)

    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
    )
    job = client.load_table_from_json(bq_rows, table_ref, job_config=job_config)
    job.result()
    return len(bq_rows)


def load_csv(csv_path: str) -> list[dict]:
    """Read an acquisition seed CSV and return rows as a list of dicts."""
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
        raise ValueError(
            f"Unknown profile: {profile!r} -- expected 'session' or 'first_user'"
        )

    pull_id = _mint_pull_id()
    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")

    rows = _rows_override if _rows_override is not None else load_csv(csv_path)

    if mode == "duckdb":
        count = load_acquisition_duckdb(
            profile, rows, pull_id, loaded_at, duckdb_path, project_id=project_id
        )
    elif mode == "bigquery":
        if not bq_project:
            raise ValueError("--bq-project is required for BigQuery mode")
        count = load_acquisition_bigquery(
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
        description="Load GA4 acquisition seed CSV into local or BigQuery warehouse"
    )
    parser.add_argument(
        "--profile",
        choices=["session", "first_user"],
        required=True,
        help="Which acquisition profile to load (session = last click; first_user = first click)",
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
        "session": str(here / "ga4_acquisition_session_seed.csv"),
        "first_user": str(here / "ga4_acquisition_first_user_seed.csv"),
    }[args.profile]

    pull_id, count = run(
        profile=args.profile,
        csv_path=args.csv_path or default_csv,
        mode=args.mode,
        duckdb_path=args.duckdb_path,
        bq_project=args.bq_project,
    )
    print(f"Loaded {count} {args.profile} acquisition rows  pull_id={pull_id}")


if __name__ == "__main__":
    main()
