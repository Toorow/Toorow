"""BigQuery replication seed loader -- Story 53.10.

Lands a deterministic set of ``raw_bigquery_daily`` rows into a DuckDB file so
``stg_bigquery_daily`` can build locally. Without it the model exists and
nothing has ever produced a row through it: the connector counts as "covered"
while the warehouse half of its path is unverified (CAV-21).

SHAPE COMES FROM THE CONNECTOR, NOT FROM THIS FILE. The rows are derived from
``tests/fixtures/golden_pull.json`` -- the same fixture the conformance suite
asserts the pull against. That fixture is a slice of a customer-owned table
(wide: one column per numeric field), and ``connector.transform`` folds it into
long format: one row per numeric column per day. The fold is reproduced here
rather than hardcoded, so a fixture that gains a column gains seed rows.

Nothing is invented: every column the model reads comes from the fixture or
from the load itself. ``breakdown_dimension`` and ``breakdown_value`` are empty
because the fixture declares no breakdown column -- that is exactly what
``transform`` writes when ``breakdown_column`` is unset, and inventing a
breakdown would claim a table shape the fixture does not have.

Append-only (AD-7): each invocation mints a fresh ``pull_id`` and never
overwrites. The staging model supersedes on ``pull_id DESC``, so re-running is
safe and idempotent in effect.

Usage:
    uv run python server/modules/bigquery/seeds/load_bigquery_seed.py \
        --duckdb-path server/modules/bigquery/seeds/local.duckdb
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from ulid import ULID

_FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "golden_pull.json"

RAW_TABLE = "raw_bigquery_daily"

#: Exactly the columns `stg_bigquery_daily.sql` selects, in that order --
#: itself a copy of connector._RAW_COLUMNS in DuckDB spelling (STRING ->
#: VARCHAR, FLOAT -> DOUBLE). Declared here so a model change breaks the load
#: loudly instead of producing a view with a silently missing column.
_CREATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {RAW_TABLE} (
    date                 VARCHAR,
    metric               VARCHAR,
    value                DOUBLE,
    breakdown_dimension  VARCHAR,
    breakdown_value      VARCHAR,
    pull_id              VARCHAR,
    loaded_at            VARCHAR,
    project_id           VARCHAR
)
"""

_INSERT_SQL = f"""
INSERT INTO {RAW_TABLE}
    (date, metric, value, breakdown_dimension, breakdown_value, pull_id,
     loaded_at, project_id)
VALUES (?,?,?,?,?,?,?,?)
"""

#: connector.transform's default date column. Every other column of the
#: replicated table is a candidate metric, which is why the fold below excludes
#: this one by name rather than listing the metrics.
_DATE_COLUMN = "date"


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def generate_rows(project_id: str = "default") -> list[dict]:
    """Fold the golden pull into one row per (day, metric).

    That IS the model's grain -- the uniqueness test spans (project_id, date,
    metric, breakdown_dimension, breakdown_value) -- so producing anything
    coarser would make the seed pass a test the real pull would fail.

    `[:10]` mirrors `transform`: a billing-export date column is a TIMESTAMP,
    and the warehouse invariant is one row per DAY, never per hour.
    """
    rows: list[dict] = []
    for item in json.loads(_FIXTURE.read_text(encoding="utf-8")):
        date_text = str(item.get(_DATE_COLUMN) or "")[:10]
        for metric, value in item.items():
            if metric == _DATE_COLUMN or value is None:
                continue
            rows.append(
                {
                    "date": date_text,
                    "metric": metric,
                    "value": float(value),
                    # No breakdown column was selected -- what transform writes.
                    "breakdown_dimension": "",
                    "breakdown_value": "",
                    "project_id": project_id,
                }
            )
    return rows


def load_duckdb(rows: list[dict], pull_id: str, loaded_at: str, duckdb_path: str) -> int:
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path)
    try:
        con.execute(_CREATE_DDL)
        con.executemany(
            _INSERT_SQL,
            [
                (
                    row["date"], row["metric"], row["value"],
                    row["breakdown_dimension"], row["breakdown_value"],
                    pull_id, loaded_at, row["project_id"],
                )
                for row in rows
            ],
        )
    finally:
        con.close()
    return len(rows)


def run(*, duckdb_path: str, days: int = 30, project_id: str = "default") -> tuple[str, int]:
    """The entry point the seed-to-mart fixture calls.

    `days` is accepted and unused: the rows are whatever the customer's table
    holds, and the fixture pins that. There is no window to widen -- a replicated
    table is replayed, not generated. Taking the argument keeps every loader
    callable the same way, which is what lets the fixture discover them instead
    of hardcoding each one.
    """
    del days
    pull_id = _mint_pull_id()
    loaded_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    rows = generate_rows(project_id=project_id)
    return pull_id, load_duckdb(rows, pull_id, loaded_at, duckdb_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duckdb-path", required=True)
    parser.add_argument("--project-id", default="default")
    args = parser.parse_args()
    pull_id, count = run(duckdb_path=args.duckdb_path, project_id=args.project_id)
    print(f"bigquery seed loaded: pull_id={pull_id} rows={count}")


if __name__ == "__main__":
    main()
