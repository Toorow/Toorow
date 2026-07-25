"""GA4 transactions seed loader -- Epic 17, story 17.4 (orchestrator gap-fill).

Loads generate_transactions_rows() (correlated with the Shopify seed) into the local
DuckDB raw table raw_ga4_transactions_daily, stamping pull_id (ULID, prefixed pull_
per AD-7) + loaded_at. Append-only, never overwrite. Mirrors load_seed_acquisition.py.

Usage:
    python load_seed_transactions.py --duckdb-path local.duckdb
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from ulid import ULID

sys.path.insert(0, str(Path(__file__).parent))
import generate_seed  # noqa: E402

_DDL = """
CREATE TABLE IF NOT EXISTS raw_ga4_transactions_daily (
    date             VARCHAR,
    transaction_id   VARCHAR,
    conversions      INTEGER,
    purchase_revenue DOUBLE,
    pull_id          VARCHAR,
    loaded_at        VARCHAR,
    project_id       VARCHAR
)
"""

_INSERT = """
INSERT INTO raw_ga4_transactions_daily
    (date, transaction_id, conversions, purchase_revenue,
     pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?)
"""


def run(duckdb_path: str, project_id: str = "default") -> tuple[str, int]:
    rows = generate_seed.generate_transactions_rows()
    pull_id = f"pull_{ULID()}"
    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    import duckdb

    con = duckdb.connect(duckdb_path)
    con.execute(_DDL)
    con.executemany(
        _INSERT,
        [
            (
                r["date"], r.get("transaction_id"), int(r["conversions"]),
                float(r["purchase_revenue"]), pull_id, loaded_at, project_id,
            )
            for r in rows
        ],
    )
    con.close()
    return pull_id, len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--duckdb-path",
        default=os.path.join(os.path.dirname(__file__), "local.duckdb"),
    )
    parser.add_argument("--project-id", default="default")
    parser.add_argument("--end-date", default=None, help="ISO date (default: today)")
    args = parser.parse_args()

    if args.end_date:
        end = date.fromisoformat(args.end_date)
        rows = generate_seed.generate_transactions_rows(end_date=end)
        pull_id = f"pull_{ULID()}"
        loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
        import duckdb

        con = duckdb.connect(args.duckdb_path)
        con.execute(_DDL)
        con.executemany(
            _INSERT,
            [
                (
                    r["date"], r.get("transaction_id"), int(r["conversions"]),
                    float(r["purchase_revenue"]), pull_id, loaded_at, args.project_id,
                )
                for r in rows
            ],
        )
        con.close()
        print(f"Loaded {len(rows)} GA4 transaction rows  pull_id={pull_id} -> {args.duckdb_path}")
        return 0
    else:
        pull_id, count = run(args.duckdb_path, project_id=args.project_id)
        print(f"Loaded {count} GA4 transaction rows  pull_id={pull_id} -> {args.duckdb_path}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
