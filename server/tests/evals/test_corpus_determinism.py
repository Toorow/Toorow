"""T1: Meta-test for two-run determinism gate (Story 14.1).

Executes every reference_sql in corpus.yaml twice in succession on the seeded DuckDB
and asserts that both executions produce bit-for-bit identical result sets.

Skips gracefully if TOOROW_DUCKDB_PATH is absent or file does not exist.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import duckdb
import pytest
import yaml

DEFAULT_DUCKDB = (
    Path(__file__).parents[2] / "modules" / "google-analytics" / "seeds" / "local.duckdb"
)


def _get_duckdb_path() -> Path | None:
    env_path = os.environ.get("TOOROW_DUCKDB_PATH")
    if env_path:
        p = Path(env_path)
        return p if p.is_file() else None
    return DEFAULT_DUCKDB if DEFAULT_DUCKDB.is_file() else None


def test_two_run_determinism():
    db_path = _get_duckdb_path()
    if not db_path:
        pytest.skip("TOOROW_DUCKDB_PATH not set or file not found — determinism gate skipped")

    evals_dir = Path(__file__).parent
    corpus_path = evals_dir / "corpus.yaml"
    data = yaml.safe_load(corpus_path.read_text(encoding="utf-8"))

    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE SCHEMA IF NOT EXISTS marts")
    marts_tables = conn.execute("SHOW TABLES FROM main_marts").fetchall()
    for (tbl,) in marts_tables:
        conn.execute(f"CREATE VIEW IF NOT EXISTS marts.{tbl} AS SELECT * FROM main_marts.{tbl}")

    for q in data.get("questions", []):
        q_id = q["id"]
        for idx, q_entry in enumerate(q.get("reference_queries", [])):
            sql = q_entry["reference_sql"]

            # Run 1
            rel1 = conn.execute(sql)
            cols1 = [desc[0] for desc in rel1.description] if rel1.description else []
            rows1 = rel1.fetchall()

            # Run 2
            rel2 = conn.execute(sql)
            cols2 = [desc[0] for desc in rel2.description] if rel2.description else []
            rows2 = rel2.fetchall()

            assert cols1 == cols2, (
                f"Question '{q_id}' query [{idx}] columns differed between run 1 and run 2"
            )
            assert (
                len(rows1) == len(rows2)
            ), f"Question '{q_id}' query [{idx}] row count differed between run 1 and run 2"


            str1 = json.dumps(rows1, default=str, sort_keys=True)
            str2 = json.dumps(rows2, default=str, sort_keys=True)
            assert str1 == str2, (
                f"Question '{q_id}' query [{idx}] result payload differed between run 1 and run 2"
            )
