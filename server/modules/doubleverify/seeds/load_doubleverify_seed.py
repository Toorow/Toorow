"""DoubleVerify daily seed loader -- Story 53.10.

Lands a deterministic set of ``raw_doubleverify_daily`` rows into a DuckDB file
so ``stg_doubleverify_daily`` can build locally. Without it the model exists and
nothing has ever produced a row through it: the connector counts as "covered"
while the warehouse half of its path is unverified (CAV-21).

SHAPE COMES FROM THE CONNECTOR, NOT FROM THIS FILE. The rows are derived from
``tests/fixtures/golden_pull.json`` -- the same fixture the conformance suite
asserts the pull against -- and put through the same two steps the connector
applies before landing: ``transform()`` drops every ratio (AD-4), then
``_melt_wide_to_long()`` emits one row per metric with the single most specific
present dimension as the breakdown pair.

Nothing is invented here: every column the model selects is either in the
fixture or produced by those two steps.

Append-only (AD-7): each invocation mints a fresh ``pull_id`` and never
overwrites. The staging model supersedes on ``pull_id DESC``, so re-running is
safe and idempotent in effect.

Usage:
    uv run python server/modules/doubleverify/seeds/load_doubleverify_seed.py \
        --duckdb-path server/modules/google-analytics/seeds/local.duckdb
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from ulid import ULID

_FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "golden_pull.json"

RAW_TABLE = "raw_doubleverify_daily"

#: `connector._DIMENSION_FIELDS` -- the columns that describe the row instead of
#: measuring it. Never melted into a metric.
_DIMENSION_FIELDS = frozenset(
    {
        "date",
        "advertiser_name",
        "campaign",
        "media_property",
        "media_type",
        "delivery_country",
        "device_delivery_type",
    }
)

#: `connector._BREAKDOWN_PRIORITY` -- most specific first. fact_daily_kpi stores
#: exactly one (breakdown_dimension, breakdown_value) pair per row, so the melt
#: must pick one and always the same one.
_BREAKDOWN_PRIORITY = (
    "campaign",
    "media_property",
    "advertiser_name",
    "delivery_country",
    "device_delivery_type",
)

#: Exactly the columns `stg_doubleverify_daily.sql` selects, in its order.
#: Declared here so a model change breaks the load loudly instead of producing a
#: view with a silently missing column.
_CREATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {RAW_TABLE} (
    date                VARCHAR,
    metric              VARCHAR,
    value               DOUBLE,
    breakdown_dimension VARCHAR,
    breakdown_value     VARCHAR,
    pull_id             VARCHAR,
    loaded_at           VARCHAR,
    project_id          VARCHAR
)
"""


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def _is_ratio(field_id: str) -> bool:
    """`connector.transform()`'s AD-4 rule, verbatim.

    A rate is never stored: summing one is meaningless. The semantic layer
    recomputes it from the numerator/denominator counts that ARE stored.
    """
    return (
        field_id.endswith("_rate")
        or field_id.startswith("rate_")
        or field_id.startswith("average_")
        or field_id.startswith("avg_")
    )


def generate_rows(project_id: str = "default") -> list[dict]:
    """Melt the golden pull into one row per (date, metric, breakdown).

    That IS the model's grain -- `stg_doubleverify_daily` is unique on
    `project_id|date|metric|breakdown_dimension|breakdown_value` -- so producing
    anything coarser would make the seed pass a test the real pull would fail.
    """
    wide_rows = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    rows: list[dict] = []
    for wide_row in wide_rows:
        breakdown_dimension = ""
        breakdown_value = ""
        for dimension in _BREAKDOWN_PRIORITY:
            if wide_row.get(dimension) not in (None, ""):
                breakdown_dimension = dimension
                breakdown_value = str(wide_row[dimension])
                break
        for key, value in wide_row.items():
            if key in _DIMENSION_FIELDS or _is_ratio(key):
                continue
            if value is None:
                continue  # AD-9: an absent metric is NULL, not zero.
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                # Fixture provenance keys (pull_id, connector) land here, as
                # they do in the connector: non-numeric, so not a measurement.
                continue
            rows.append(
                {
                    "date": str(wide_row.get("date", "")),
                    "metric": key,
                    "value": numeric,
                    "breakdown_dimension": breakdown_dimension,
                    "breakdown_value": breakdown_value,
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
            f"INSERT INTO {RAW_TABLE} VALUES (?,?,?,?,?,?,?,?)",
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

    `days` is accepted and unused: the golden pull pins its own measurement
    dates, and widening the window would mean fabricating DoubleVerify rows.
    Taking the argument keeps every loader callable the same way, which is what
    lets the fixture discover them instead of hardcoding each one.
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
    print(f"doubleverify seed loaded: pull_id={pull_id} rows={count}")


if __name__ == "__main__":
    main()
