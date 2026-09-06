"""Load the Instagram golden fixture into a local DuckDB raw table."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb
from ulid import ULID

MODULE_DIR = Path(__file__).resolve().parents[1]
FIXTURE = MODULE_DIR / "tests" / "fixtures" / "golden_pull.json"
MANIFEST = MODULE_DIR / "manifest.json"

DDL = """
CREATE TABLE IF NOT EXISTS raw_instagram_insights_daily (
 report_profile VARCHAR, date VARCHAR, account_id VARCHAR, media_id VARCHAR,
 media_type VARCHAR, media_product_type VARCHAR, permalink VARCHAR, caption VARCHAR,
 published_at VARCHAR, metric VARCHAR, value DOUBLE, non_additive BOOLEAN,
 payload_json VARCHAR, report_timezone VARCHAR, pull_id VARCHAR, loaded_at VARCHAR,
 project_id VARCHAR
)
"""

INSERT = (
    "INSERT INTO raw_instagram_insights_daily "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def load(database: Path, project_id: str, pull_id: str) -> int:
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    mappings = manifest["canonical_metric_mapping"]
    loaded_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    values = []
    for row in rows:
        for source_field, target in mappings.items():
            value = row.get(source_field)
            if not isinstance(value, (int, float)):
                continue
            canonical = target if isinstance(target, str) else target["canonical"]
            non_additive = False if isinstance(target, str) else target.get("non_additive", False)
            values.append(
                (
                    row.get("report_profile"),
                    row.get("date"),
                    row.get("account_id"),
                    row.get("media_id"),
                    row.get("media_type"),
                    row.get("media_product_type"),
                    row.get("permalink"),
                    row.get("caption"),
                    row.get("published_at"),
                    canonical,
                    float(value),
                    bool(non_additive),
                    json.dumps(row, sort_keys=True, separators=(",", ":")),
                    None,
                    pull_id,
                    loaded_at,
                    project_id,
                )
            )
    connection = duckdb.connect(str(database))
    try:
        connection.execute(DDL)
        connection.executemany(INSERT, values)
    finally:
        connection.close()
    return len(values)


def run(*, duckdb_path: str, project_id: str = "default") -> tuple[str, int]:
    # AD-7 provenance: a pull_id is `pull_<ULID>`, never a bare ULID. This loader
    # was the only one of the 40 to mint `str(ULID())`, so the row it landed was
    # the single failure of the `fact_kpi_pull_id_in_raw` gate on the whole mart
    # (measured 2026-08-04). Production was never affected -- the real pull_id
    # comes from `core/queue.py:86`, which already prefixes -- but a seed that
    # mints a shape the gate rejects makes the gate red for a fiction.
    pull_id = f"pull_{ULID()}"
    count = load(Path(duckdb_path), project_id, pull_id)
    return pull_id, count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duckdb-path", required=True)
    parser.add_argument("--project-id", default="instagram-seed")
    args = parser.parse_args()
    pull_id, count = run(duckdb_path=args.duckdb_path, project_id=args.project_id)
    print(f"instagram-insights seed loaded: pull_id={pull_id} rows={count}")


if __name__ == "__main__":
    main()
