"""Unit tests for the GA4 seed loader (DuckDB mode) — Story 1.4, T3.4.

All tests use a temporary DuckDB file (tmp_path fixture) — no live GCP needed.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

import pytest

_SEEDS_DIR = Path(__file__).parents[4] / "server" / "modules" / "google-analytics" / "seeds"


def _import_load_seed():
    spec = importlib.util.spec_from_file_location("load_seed", _SEEDS_DIR / "load_seed.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def loader():
    return _import_load_seed()


def _make_rows(n: int = 10) -> list[dict]:
    """Generate *n* synthetic rows for testing."""
    return [
        {
            "date": "2026-04-01",
            "device_category": "desktop",
            "country": "France",
            "sessions": str(100 + i),
            "active_users": str(80 + i),
            "conversions": str(5 + i),
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_all_rows_same_pull_id(loader, tmp_path):
    """All rows from a single load invocation share the same pull_id (AD-7)."""
    import duckdb

    db_path = str(tmp_path / "test.duckdb")
    pull_id, count = loader.run(
        csv_path="",
        mode="duckdb",
        duckdb_path=db_path,
        bq_project=None,
        _rows_override=_make_rows(10),
    )
    con = duckdb.connect(db_path, read_only=True)
    ids = {
        row[0]
        for row in con.execute("SELECT DISTINCT pull_id FROM raw_ga4_standard_daily").fetchall()
    }
    con.close()
    assert len(ids) == 1
    assert ids.pop() == pull_id


def test_pull_id_starts_with_pull_(loader, tmp_path):
    """pull_id must start with 'pull_' (AD-7, Architecture Spine §Consistency)."""
    db_path = str(tmp_path / "test_prefix.duckdb")
    pull_id, _ = loader.run(
        csv_path="",
        mode="duckdb",
        duckdb_path=db_path,
        bq_project=None,
        _rows_override=_make_rows(5),
    )
    assert pull_id.startswith("pull_"), f"pull_id {pull_id!r} does not start with 'pull_'"


def test_append_only_doubles_row_count(loader, tmp_path):
    """Running the loader twice doubles the row count (append-only, never overwrite, AD-7)."""
    import duckdb

    db_path = str(tmp_path / "test_append.duckdb")
    rows = _make_rows(10)

    loader.run(
        csv_path="", mode="duckdb", duckdb_path=db_path, bq_project=None, _rows_override=rows
    )
    loader.run(
        csv_path="", mode="duckdb", duckdb_path=db_path, bq_project=None, _rows_override=rows
    )

    con = duckdb.connect(db_path, read_only=True)
    count = con.execute("SELECT COUNT(*) FROM raw_ga4_standard_daily").fetchone()[0]
    con.close()
    assert count == 20


def test_two_runs_have_different_pull_ids(loader, tmp_path):
    """Two separate invocations must produce different pull_ids."""
    db_path = str(tmp_path / "test_two_pull.duckdb")
    rows = _make_rows(5)
    id1, _ = loader.run(
        csv_path="", mode="duckdb", duckdb_path=db_path, bq_project=None, _rows_override=rows
    )
    id2, _ = loader.run(
        csv_path="", mode="duckdb", duckdb_path=db_path, bq_project=None, _rows_override=rows
    )
    assert id1 != id2


def test_loaded_at_is_utc_iso8601(loader, tmp_path):
    """loaded_at must be parseable as a UTC ISO-8601 timestamp."""
    import duckdb

    db_path = str(tmp_path / "test_loaded_at.duckdb")
    loader.run(
        csv_path="",
        mode="duckdb",
        duckdb_path=db_path,
        bq_project=None,
        _rows_override=_make_rows(3),
    )

    con = duckdb.connect(db_path, read_only=True)
    loaded_ats = [
        r[0]
        for r in con.execute("SELECT DISTINCT loaded_at FROM raw_ga4_standard_daily").fetchall()
    ]
    con.close()

    assert len(loaded_ats) == 1
    ts = loaded_ats[0]
    # Must end with 'Z' and be parseable
    assert ts.endswith("Z"), f"loaded_at {ts!r} does not end with 'Z'"
    # Parse: replace 'Z' with '+00:00' for Python 3.10 compat
    parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


def test_row_count_returned(loader, tmp_path):
    """run() returns the correct loaded row count."""
    db_path = str(tmp_path / "test_count.duckdb")
    rows = _make_rows(7)
    _, count = loader.run(
        csv_path="", mode="duckdb", duckdb_path=db_path, bq_project=None, _rows_override=rows
    )
    assert count == 7
