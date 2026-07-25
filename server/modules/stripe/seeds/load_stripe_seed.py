"""Stripe payments seed loader — Story 15.7 (Epic 15).

Lands a deterministic set of ``raw_stripe_payments`` rows into a DuckDB file so the
local dev loop (and the seed->mart integration test) prove the Stripe join end-to-end:
rows appear in ``fact_daily_kpi`` with ``connector = 'stripe'`` after ``dbt run``.

Mirrors the shopify / tiktok seed loader shape. Append-only (AD-7): each invocation mints
a fresh pull_id and never overwrites existing rows. ASCII-only stdout (AI-03).

WINDOW ALIGNMENT: generate_rows() defaults to end_date=date.today(). This loader calls
generate_rows() without an explicit end_date so both Shopify and Stripe seeds share the
same today()-anchored window. At runtime the Stripe charges land on the same date range
as the Shopify orders already in raw_shopify_orders -> the dedup overlap check (Guard A)
remains valid indefinitely, and client_reference_id joins are REAL (not tautological).

Usage:
    uv run python server/modules/stripe/seeds/load_stripe_seed.py \
        --duckdb-path server/modules/google-analytics/seeds/local.duckdb
"""

from __future__ import annotations

import argparse
import os

# Reuse the generator so the seed rows are the canonical parse-shape (AI-54).
import sys as _sys
from datetime import datetime, timezone
from pathlib import Path

from ulid import ULID

_sys.path.insert(0, str(Path(__file__).parent))
from generate_stripe_seed import generate_rows  # noqa: E402

_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_stripe_payments (
    date                     VARCHAR,
    charge_id                VARCHAR,
    payment_intent_id        VARCHAR,
    client_reference_id      VARCHAR,
    revenue                  DOUBLE,
    refunds                  DOUBLE,
    fees                     DOUBLE,
    transaction_count        INTEGER,
    order_count              INTEGER,
    revenue_source_currency  VARCHAR DEFAULT 'EUR',
    pull_id                  VARCHAR,
    loaded_at                VARCHAR,
    project_id               VARCHAR
)
"""

_ALTER_ADD_CURRENCY = (
    "ALTER TABLE raw_stripe_payments ADD COLUMN IF NOT EXISTS "
    "revenue_source_currency VARCHAR DEFAULT 'EUR'"
)
_ALTER_ADD_CLIENT_REF = (
    "ALTER TABLE raw_stripe_payments ADD COLUMN IF NOT EXISTS client_reference_id VARCHAR"
)

_INSERT_SQL = """
INSERT INTO raw_stripe_payments
    (date, charge_id, payment_intent_id, client_reference_id, revenue, refunds, fees,
     transaction_count, order_count, revenue_source_currency, pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def load_duckdb(
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    duckdb_path: str,
    project_id: str = "default",
) -> int:
    """Insert *rows* into raw_stripe_payments in a DuckDB file (append-only)."""
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path)
    con.execute(_CREATE_DDL)
    try:
        con.execute(_ALTER_ADD_CURRENCY)
    except Exception:
        pass  # column already present -- safe to ignore.
    try:
        con.execute(_ALTER_ADD_CLIENT_REF)
    except Exception:
        pass  # column already present -- safe to ignore.
    values = [
        (
            r["date"],
            r["charge_id"],
            r.get("payment_intent_id"),
            r.get("client_reference_id"),  # None -> NULL (Stripe-only charges)
            float(r["revenue"]),
            float(r["refunds"]),
            float(r["fees"]),
            int(r["transaction_count"]),
            int(r["order_count"]),
            r.get("revenue_source_currency", "EUR"),
            pull_id,
            loaded_at,
            project_id,
        )
        for r in rows
    ]
    con.executemany(_INSERT_SQL, values)
    con.close()
    return len(values)


def run(duckdb_path: str, days: int = 90, project_id: str = "default") -> tuple[str, int]:
    """Generate + load Stripe seed rows. Returns (pull_id, row_count)."""
    pull_id = _mint_pull_id()
    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    rows = generate_rows(days=days)
    count = load_duckdb(rows, pull_id, loaded_at, duckdb_path, project_id=project_id)
    return pull_id, count


def main() -> None:
    here = Path(__file__).parent
    default_duckdb = os.environ.get(
        "TOOROW_DUCKDB_PATH",
        str(here.parents[1] / "google-analytics" / "seeds" / "local.duckdb"),
    )
    parser = argparse.ArgumentParser(description="Load Stripe payments seed rows into DuckDB")
    parser.add_argument("--duckdb-path", default=default_duckdb, help="DuckDB file path")
    parser.add_argument("--days", type=int, default=90, help="Number of days to seed")
    args = parser.parse_args()

    pull_id, count = run(args.duckdb_path, days=args.days)
    print(f"Loaded {count} Stripe rows  pull_id={pull_id}  -> {args.duckdb_path}")


if __name__ == "__main__":
    main()
