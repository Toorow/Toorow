"""monday board-snapshot seed loader — Story 53.10.

Lands a deterministic set of ``raw_monday_board_snapshot`` rows into a DuckDB
file so ``stg_monday_board_snapshot`` can build locally. Without it the model
exists and nothing has ever produced a row through it: the connector counts as
"covered" while the warehouse half of its path is unverified (CAV-21).

SHAPE COMES FROM THE CONNECTOR, NOT FROM THIS FILE. The rows are derived from
``tests/fixtures/golden_pull.json`` -- the same fixture the conformance suite
asserts the pull against -- so the seed cannot drift from what the connector
actually returns. Fields the model requires and the fixture does not carry
(``item_name``, ``column_text``) are derived deterministically from the ids and
are obviously synthetic; they are never dressed as provider values.

Append-only (AD-7): each invocation mints a fresh ``pull_id`` and never
overwrites. The staging model supersedes on ``pull_id DESC``, so re-running is
safe and idempotent in effect.

Usage:
    uv run python server/modules/monday/seeds/load_monday_seed.py \
        --duckdb-path server/modules/google-analytics/seeds/local.duckdb
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from ulid import ULID

_FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "golden_pull.json"

RAW_TABLE = "raw_monday_board_snapshot"

#: Exactly the columns `stg_monday_board_snapshot.sql` selects. Declared here so
#: a model change breaks the load loudly instead of producing a view with a
#: silently missing column.
_CREATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {RAW_TABLE} (
    board_id            VARCHAR,
    item_id             VARCHAR,
    item_name           VARCHAR,
    group_id            VARCHAR,
    parent_item_id      VARCHAR,
    updated_at          VARCHAR,
    column_id           VARCHAR,
    column_type         VARCHAR,
    column_text         VARCHAR,
    column_raw_value    VARCHAR,
    known_type          BOOLEAN,
    schema_fingerprint  VARCHAR,
    pull_id             VARCHAR,
    loaded_at           VARCHAR,
    project_id          VARCHAR
)
"""

#: The column types monday's own contract recognises. Anything else lands with
#: `known_type = false`, which is the honest value: the connector saw a column
#: whose type it does not model, and the warehouse must be able to say so.
_KNOWN_COLUMN_TYPES = frozenset({"status", "text", "numbers", "date", "people", "dropdown"})


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def generate_rows(project_id: str = "default") -> list[dict]:
    """Flatten the golden pull into one row per (item, column).

    That IS the model's grain -- `stg_monday_board_snapshot` is unique on
    `project_id|board_id|item_id|column_id` -- so producing anything coarser
    would make the seed pass a test the real pull would fail.
    """
    items = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    rows: list[dict] = []
    for item in items:
        board_id = str(item.get("board_id") or "")
        item_id = str(item.get("item_id") or "")
        columns = item.get("column_values") or []
        # The fingerprint is over the column SHAPE of this item, so a provider
        # adding or renaming a column changes it. Sorted: a fingerprint that
        # depends on response ordering would churn without a schema change.
        fingerprint = "|".join(
            sorted(f"{column.get('id')}:{column.get('type')}" for column in columns)
        )
        for column in columns:
            column_id = str(column.get("id") or "")
            column_type = str(column.get("type") or "")
            raw_value = column.get("raw_value")
            rows.append(
                {
                    "board_id": board_id,
                    "item_id": item_id,
                    # Not in the fixture: derived, and deliberately shaped so no
                    # reader mistakes it for a real board item name.
                    "item_name": f"seed item {item_id}",
                    "group_id": str(item.get("group_id") or ""),
                    "parent_item_id": str(item.get("parent_item_id") or ""),
                    "updated_at": str(item.get("updated_at") or ""),
                    "column_id": column_id,
                    "column_type": column_type,
                    "column_text": f"seed {column_id}",
                    "column_raw_value": json.dumps(raw_value, sort_keys=True),
                    "known_type": column_type in _KNOWN_COLUMN_TYPES,
                    "schema_fingerprint": fingerprint,
                    "project_id": project_id,
                }
            )
    return rows


def load_duckdb(
    rows: list[dict], pull_id: str, loaded_at: str, duckdb_path: str
) -> int:
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path)
    try:
        con.execute(_CREATE_DDL)
        con.executemany(
            f"INSERT INTO {RAW_TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    row["board_id"], row["item_id"], row["item_name"], row["group_id"],
                    row["parent_item_id"], row["updated_at"], row["column_id"],
                    row["column_type"], row["column_text"], row["column_raw_value"],
                    row["known_type"], row["schema_fingerprint"],
                    pull_id, loaded_at, row["project_id"],
                )
                for row in rows
            ],
        )
    finally:
        con.close()
    return len(rows)


def run(
    *, duckdb_path: str, days: int = 30, project_id: str = "default"
) -> tuple[str, int]:
    """The entry point the seed-to-mart fixture calls.

    `days` is accepted and unused: monday's snapshot is a state, not a daily
    series. Taking the argument keeps every loader callable the same way, which
    is what lets the fixture discover them instead of hardcoding each one.
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
    print(f"monday seed loaded: pull_id={pull_id} rows={count}")


if __name__ == "__main__":
    main()
