"""Generic tabular seed loader -- Story 53.10.

Lands a deterministic set of ``raw_generic_daily`` rows into a DuckDB file so
``stg_generic_daily`` can build locally. Without it the model exists and nothing
has ever produced a row through it: the connector counts as "covered" while the
warehouse half of its path is unverified (CAV-21).

THE RAW TABLE IS FIXED EVEN THOUGH THE CONNECTOR IS GENERIC. What varies from
one generic datastream to the next is the *columns of the uploaded file*, never
the landing table: ``_ensure_raw_table()`` always writes ``raw_generic_daily``
in long format, and the dbt source names it explicitly. So a single seed table
is the honest one.

SHAPE COMES FROM THE CONNECTOR, NOT FROM THIS FILE. The rows are derived from
``tests/fixtures/golden_pull.json`` -- the same fixture the conformance suite
asserts ``transform()`` against -- exploded exactly as ``_explode_row()``
explodes it: numeric non-date columns are measures (sorted, for determinism),
the first remaining column is the breakdown.

Append-only (AD-7): each invocation mints a fresh ``pull_id`` and never
overwrites. The staging model supersedes on ``pull_id DESC``, so re-running is
safe and idempotent in effect.

Usage:
    uv run python server/modules/generic/seeds/load_generic_seed.py \
        --duckdb-path server/modules/google-analytics/seeds/local.duckdb
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ulid import ULID

_FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "golden_pull.json"

RAW_TABLE = "raw_generic_daily"

#: `connector._detect_date_column()`'s vocabulary. The seed must detect the date
#: column the same way, or it would land a date as a breakdown value.
_DATE_COLUMN_NAMES = ("date", "jour", "day", "datum", "fecha")

#: Exactly the columns `stg_generic_daily.sql` selects, in its order -- note
#: `project_id` comes FIRST here, unlike every other connector's landing table.
#: Declared so a model change breaks the load loudly instead of producing a view
#: with a silently missing column.
_CREATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {RAW_TABLE} (
    project_id          VARCHAR,
    date                VARCHAR,
    metric              VARCHAR,
    value               DOUBLE,
    breakdown_dimension VARCHAR,
    breakdown_value     VARCHAR,
    pull_id             VARCHAR,
    loaded_at           VARCHAR
)
"""


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def _detect_date_column(row: dict) -> str | None:
    for key in row:
        if key.lower() in _DATE_COLUMN_NAMES:
            return key
    return None


def _is_numeric(value: Any) -> bool:
    """`connector._is_numeric()`, verbatim: numeric-ness is what makes a measure.

    The generic connector has no catalogue to consult -- the file's own values
    decide which column is a metric and which is a dimension.
    """
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value.replace(",", "."))
            return True
        except (ValueError, TypeError):
            return False
    return False


def generate_rows(project_id: str = "default") -> list[dict]:
    """Explode the golden pull into one row per (date, metric, breakdown).

    That IS the model's grain -- `stg_generic_daily` supersedes on
    `project_id, date, metric, breakdown_dimension, breakdown_value` -- so
    producing anything coarser would make the seed pass a test the real pull
    would fail.
    """
    wide_rows = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    rows: list[dict] = []
    for wide_row in wide_rows:
        date_col = _detect_date_column(wide_row)
        if not date_col:
            continue
        measure_cols = {
            key for key, value in wide_row.items() if key != date_col and _is_numeric(value)
        }
        dimension_cols = [
            key for key in wide_row if key != date_col and key not in measure_cols
        ]
        # v1: first dimension wins. The mart stores exactly one breakdown pair
        # per row, so the melt must pick one and always the same one.
        breakdown_dimension = dimension_cols[0] if dimension_cols else None
        breakdown_value = str(wide_row[dimension_cols[0]]) if dimension_cols else None
        for column in sorted(measure_cols):  # sorted: deterministic row order
            rows.append(
                {
                    "project_id": project_id,
                    "date": str(wide_row[date_col]),
                    "metric": column,
                    "value": float(str(wide_row[column]).replace(",", ".")),
                    "breakdown_dimension": breakdown_dimension,
                    "breakdown_value": breakdown_value,
                }
            )
    return rows


def load_duckdb(rows: list[dict], pull_id: str, loaded_at: str, duckdb_path: str) -> int:
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path)
    try:
        con.execute(_CREATE_DDL)
        con.executemany(
            f"INSERT INTO {RAW_TABLE} VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    row["project_id"], row["date"], row["metric"], row["value"],
                    row["breakdown_dimension"], row["breakdown_value"],
                    pull_id, loaded_at,
                )
                for row in rows
            ],
        )
    finally:
        con.close()
    return len(rows)


def run(*, duckdb_path: str, days: int = 30, project_id: str = "default") -> tuple[str, int]:
    """The entry point the seed-to-mart fixture calls.

    `days` is accepted and unused: a generic datastream's dates come from the
    uploaded file, and the golden pull pins its own. Taking the argument keeps
    every loader callable the same way, which is what lets the fixture discover
    them instead of hardcoding each one.
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
    print(f"generic seed loaded: pull_id={pull_id} rows={count}")


if __name__ == "__main__":
    main()
