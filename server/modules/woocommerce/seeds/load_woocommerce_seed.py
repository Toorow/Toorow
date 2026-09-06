"""WooCommerce orders seed loader -- Story 53.10.

Lands a deterministic set of ``raw_woocommerce_orders`` rows into a DuckDB file
so ``stg_woocommerce_orders_daily`` -- and the woocommerce block of
``fact_daily_kpi`` that reads it -- can build locally. Without it the models
exist and nothing has ever produced a row through them: the connector counts as
"covered" while the warehouse half of its path is unverified (CAV-21).

SHAPE COMES FROM THE CONNECTOR, NOT FROM THIS FILE. The rows are derived from
``tests/fixtures/golden_pull.json`` -- the same fixture the conformance suite
asserts the pull against -- with the two renames the connector performs
(total -> revenue, total_refunded -> refund_amount), so the seed cannot drift
from what the connector actually returns.

REFUNDS: ``refund_amount`` lands as a DEDICATED POSITIVE column (the connector
already took abs() of WooCommerce's negative refund totals) and is NEVER netted
into ``revenue`` here. Order 1005 in the fixture is fully refunded and order
1003 partially, which is what makes that separation observable downstream
rather than merely asserted.

Append-only (AD-7): each invocation mints a fresh ``pull_id`` and never
overwrites. The staging model supersedes on ``pull_id DESC``, so re-running is
safe and idempotent in effect.

Usage:
    uv run python server/modules/woocommerce/seeds/load_woocommerce_seed.py \
        --duckdb-path server/modules/google-analytics/seeds/local.duckdb
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from ulid import ULID

_FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "golden_pull.json"

RAW_TABLE = "raw_woocommerce_orders"

#: Exactly the columns `stg_woocommerce_orders_daily.sql` reads, in the
#: connector's landing order. Declared here so a model change breaks the load
#: loudly instead of producing a view with a silently missing column.
_CREATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {RAW_TABLE} (
    date                    VARCHAR,
    order_id                VARCHAR,
    transaction_id          VARCHAR,
    revenue                 DOUBLE,
    refund_amount           DOUBLE,
    orders_count            INTEGER,
    revenue_source_currency VARCHAR,
    pull_id                 VARCHAR,
    loaded_at               VARCHAR,
    project_id              VARCHAR
)
"""


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def generate_rows(project_id: str = "default") -> list[dict]:
    """One row per ORDER, which is the model's grain.

    `stg_woocommerce_orders_daily` is unique on `project_id|date|order_id`; the
    mart aggregates those orders to the day. Seeding at day grain would skip the
    exact join the staging model exists to prove.
    """
    orders = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return [
        {
            "date": str(order.get("date") or ""),
            "order_id": str(order.get("order_id") or ""),
            # Nullable by contract: a manual/offline payment carries no gateway
            # transaction id, and order 1004 in the fixture is exactly that
            # case. NULL, never an empty-string stand-in (AD-9).
            "transaction_id": (
                str(order["transaction_id"])
                if order.get("transaction_id") is not None
                else None
            ),
            "revenue": float(order.get("total") or 0.0),
            "refund_amount": float(order.get("total_refunded") or 0.0),
            "orders_count": int(order.get("orders_count") or 0),
            "revenue_source_currency": str(order.get("revenue_source_currency") or ""),
            "project_id": project_id,
        }
        for order in orders
    ]


def load_duckdb(rows: list[dict], pull_id: str, loaded_at: str, duckdb_path: str) -> int:
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path)
    try:
        con.execute(_CREATE_DDL)
        con.executemany(
            f"INSERT INTO {RAW_TABLE} VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    row["date"], row["order_id"], row["transaction_id"], row["revenue"],
                    row["refund_amount"], row["orders_count"],
                    row["revenue_source_currency"], pull_id, loaded_at, row["project_id"],
                )
                for row in rows
            ],
        )
    finally:
        con.close()
    return len(rows)


def run(*, duckdb_path: str, days: int = 30, project_id: str = "default") -> tuple[str, int]:
    """The entry point the seed-to-mart fixture calls.

    `days` is accepted and unused: the order dates come from the golden fixture,
    which is what pins the seed to the connector's real response. Sliding them
    onto a rolling window would mean inventing orders the fixture never
    observed. Taking the argument keeps every loader callable the same way,
    which is what lets the fixture discover them instead of hardcoding each one.
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
    print(f"woocommerce seed loaded: pull_id={pull_id} rows={count}")


if __name__ == "__main__":
    main()
