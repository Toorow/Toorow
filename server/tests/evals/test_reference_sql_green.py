"""T2: Meta-test for reference SQL execution against DuckDB seeds (Story 14.1).

Executes every reference_sql in corpus.yaml against local DuckDB and asserts that:
1. Every query executes without error.
2. The SHA-256 of the JSON result matches fixture_sha256.
3. Designed empty-state queries (expected_empty=true) return 0 rows.

Skips gracefully if TOOROW_DUCKDB_PATH is absent or file does not exist.
"""

from __future__ import annotations

import hashlib
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


def test_reference_sql_green_on_seeds():
    db_path = _get_duckdb_path()
    if not db_path:
        pytest.skip(
            "TOOROW_DUCKDB_PATH not set or file not found — reference SQL green gate skipped"
        )

    evals_dir = Path(__file__).parent
    corpus_path = evals_dir / "corpus.yaml"
    data = yaml.safe_load(corpus_path.read_text(encoding="utf-8"))

    conn = duckdb.connect(str(db_path))
    # Determinism: pin single-threaded, insertion-ordered aggregation so SUM() over floats
    # reduces in the SAME order as the fixture builder (build_eval_corpus_and_fixtures.py).
    # Parallel reduction reorders adds -> IEEE-754 trailing-bit noise on fractional metrics
    # (e.g. cost 27056.899999999998 vs ...987), which would make this sha-exact gate flaky.
    conn.execute("SET threads TO 1")
    conn.execute("SET preserve_insertion_order TO true")
    conn.execute("CREATE SCHEMA IF NOT EXISTS marts")
    marts_tables = conn.execute("SHOW TABLES FROM main_marts").fetchall()
    for (tbl,) in marts_tables:
        conn.execute(f"CREATE VIEW IF NOT EXISTS marts.{tbl} AS SELECT * FROM main_marts.{tbl}")

    for q in data.get("questions", []):
        q_id = q["id"]
        for idx, q_entry in enumerate(q.get("reference_queries", [])):
            sql = q_entry["reference_sql"]
            expected_sha = q_entry["fixture_sha256"]
            expected_empty = q_entry.get("expected_empty", False)
            fixture_rel = q_entry["expected_result_fixture"]

            rel = conn.execute(sql)
            cols = [desc[0] for desc in rel.description] if rel.description else []
            rows = rel.fetchall()

            row_dicts = []
            for r in rows:
                rd = {}
                for col, val in zip(cols, r):
                    if hasattr(val, "isoformat"):
                        val = val.isoformat()
                    rd[col] = val
                row_dicts.append(rd)

            # FIX 2026-07-20 (scoring-honesty): removed the COUNT=0->[] coercion heuristic.
            # A single row with all-zero/null values is a REAL result (e.g. COUNT(*) AS x = 0
            # means "no gaps found" — that is information, not an empty set). The empty-set
            # contract is expressed exclusively via expected_empty=true in corpus.yaml, which
            # covers only queries that return EXACTLY 0 rows by design (GROUP BY over absent
            # data). COUNT/SUM aggregates without GROUP BY always return one row (null/zero)
            # and must NOT be marked expected_empty; their fixture captures the real result.
            #
            # FIX 2026-07-21 (Fix 1 — strict expected_empty): remove is_null_aggregate soft
            # catch-all. expected_empty now means strictly 0 rows — no exceptions. A scored
            # (non-empty-expected) question can NEVER be satisfied by 0 rows.
            if expected_empty:
                assert len(row_dicts) == 0, (
                    f"Question '{q_id}' query [{idx}] marked expected_empty=true but returned "
                    f"{len(row_dicts)} row(s): {row_dicts}. "
                    "expected_empty is strictly 0 rows. Aggregate queries (COUNT/SUM without "
                    "GROUP BY) always return one row — drop expected_empty and pin the "
                    "real fixture."
                )

            fixture_bytes = json.dumps(
                row_dicts, sort_keys=True, indent=2, ensure_ascii=False
            ).encode("utf-8")
            actual_sha = hashlib.sha256(fixture_bytes).hexdigest()

            assert actual_sha == expected_sha, (
                f"Question '{q_id}' query [{idx}] SHA-256 mismatch for fixture '{fixture_rel}':\n"
                f"Expected: {expected_sha}\nActual:   {actual_sha}"
            )
