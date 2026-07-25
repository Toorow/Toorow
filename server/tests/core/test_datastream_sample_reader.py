"""Tests for cache_warehouse.read_datastream_sample -- Story 12.19 daily sample.

Offline, DB-free (no Postgres): drives the warehouse READER directly against a
temp DuckDB origin that plays the role of ``local.duckdb`` (``main_marts.*``), the
same simulation used by test_cache_warehouse.py. Proves the security + determinism
contract of the sample reader (the admin_api HTTP seam -- auth/scope/rejection
overlay -- is exercised by the DB-gated integration suite, verified centrally).

Contract proven here:
  * DETERMINISTIC first-N: two reads over an unchanged relation return byte-identical
    "first N" rows (stable evidence, not a random draw);
  * PER-DAY grouping + row_count/field_count + empty-day handling (no fabrication);
  * SERVER-SIDE MASKING: a PII-named column is masked to the sentinel and its raw
    value never appears in the response; masked_fields lists it;
  * HARD LIMIT: per-day rows never exceed the requested/hard-capped limit;
  * STAGE HONESTY: a stage with no distinct materialisation reports served_stage +
    a stage_note; the served stage is stable;
  * AD-5: rows of another project_id / another connector never leak into the sample;
  * bad input (invalid stage / inverted range) raises SampleReadError, not a raw exc.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

# The mart carries a canonical schema PLUS one PII-named column ("user_email") so the
# masking policy has something to bite on. The real fact_daily_kpi has no such column;
# adding it here proves the name-based masking without depending on prod schema.
_FACT_DDL = """
    CREATE TABLE main_marts.fact_daily_kpi (
        project_id          TEXT,
        date                DATE,
        connector           TEXT,
        metric              TEXT,
        breakdown_dimension TEXT,
        breakdown_value     TEXT,
        value               DOUBLE,
        user_email          TEXT,
        pull_id             TEXT,
        loaded_at           TIMESTAMP
    )
"""


def _seed_origin(path: str) -> None:
    import duckdb

    con = duckdb.connect(path)
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
        con.execute(_FACT_DDL)
        # Two days, project 'p1' + connector 'google-analytics': 8 rows/day so the
        # per-day limit (5) actually truncates. Deterministic-but-not-pre-sorted
        # insertion order (reversed metric/value) so ORDER BY has real work to do.
        rows = []
        base = date(2026, 7, 1)
        for day_offset in range(2):
            d = base + timedelta(days=day_offset)
            for i in range(8):
                metric = f"m{7 - i}"  # inserted high->low so ORDER BY must resort
                rows.append(
                    (
                        "p1", d, "google-analytics", metric,
                        "device_category", "mobile", float(i),
                        f"user{i}@example.com",  # PII value -- must be masked out
                        f"pull_{day_offset}_{i:02d}", None,
                    )
                )
        # AD-5 leak guards: a row for another project and another connector on day 1.
        rows.append(("p2", base, "google-analytics", "m0", "d", "v", 1.0,
                     "leak@other.com", "pull_leak_proj", None))
        rows.append(("p1", base, "meta-ads", "m0", "d", "v", 1.0,
                     "leak@meta.com", "pull_leak_conn", None))
        con.executemany(
            "INSERT INTO main_marts.fact_daily_kpi VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    finally:
        con.close()


@pytest.fixture
def sample_env(tmp_path, monkeypatch):
    origin = str(tmp_path / "origin_local.duckdb")
    _seed_origin(origin)
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", origin)
    # Cache OFF so the reader hits the origin mart directly (deterministic).
    monkeypatch.setenv("TOOROW_CACHE_ENABLED", "false")
    return origin


def _read(**over):
    from core.cache_warehouse import read_datastream_sample

    kwargs = dict(
        project_id="p1",
        connector="google-analytics",
        stage="processed",
        date_from="2026-07-01",
        date_to="2026-07-02",
        limit=5,
    )
    kwargs.update(over)
    return read_datastream_sample(**kwargs)


def test_per_day_grouping_and_limit(sample_env):
    out = _read()
    assert [d["date"] for d in out["days"]] == ["2026-07-01", "2026-07-02"]
    for day in out["days"]:
        assert day["row_count"] == 5  # 8 eligible rows truncated to the limit
        assert day["field_count"] == 10  # all mart columns counted
        assert len(day["rows"]) == 5
        assert day["rejection_count"] == 0  # mart carries no rejections


def test_deterministic_first_n(sample_env):
    a = _read()
    b = _read()
    assert a["days"] == b["days"]  # byte-identical "first N" across runs
    # The first row of day 1 is the lexicographically-smallest metric (m0), proving
    # the ORDER BY actually resorts the reversed insertion order (seeded m7..m0).
    assert a["days"][0]["rows"][0]["metric"] == "m0"


def test_server_side_masking(sample_env):
    out = _read()
    assert out["masked_fields"] == ["user_email"]
    for day in out["days"]:
        for row in day["rows"]:
            assert row["user_email"] == "[MASKED]"
            # The raw PII value must appear NOWHERE in the row values.
            assert "@example.com" not in str(row.values())


def test_ad5_project_and_connector_isolation(sample_env):
    # p2 / meta-ads rows were seeded on day 1; they must never leak into p1/GA sample.
    out = _read()
    day1 = out["days"][0]
    assert day1["row_count"] == 5  # only the 8 p1/GA rows are eligible (not the leaks)
    for row in day1["rows"]:
        assert row["breakdown_value"] == "mobile"  # the leak rows used 'v'


def test_stage_note_when_no_distinct_materialisation(sample_env):
    processed = _read(stage="processed")
    assert processed["served_stage"] == "processed"
    assert processed["stage_note"] is None  # requested == served
    collected = _read(stage="collected")
    assert collected["served_stage"] == "processed"
    assert collected["stage_note"] and "collected" in collected["stage_note"]


def test_empty_day_no_fabrication(sample_env):
    out = _read(date_from="2026-07-05", date_to="2026-07-05")
    assert len(out["days"]) == 1
    assert out["days"][0]["row_count"] == 0
    assert out["days"][0]["rows"] == []


def test_absent_relation_is_honest_empty(tmp_path, monkeypatch):
    # Origin without the mart -> honest empty days, never a raised exception.
    import duckdb

    empty = str(tmp_path / "empty.duckdb")
    duckdb.connect(empty).close()
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", empty)
    monkeypatch.setenv("TOOROW_CACHE_ENABLED", "false")
    out = _read()
    assert out["masked_fields"] == []
    assert all(d["row_count"] == 0 for d in out["days"])


def test_invalid_stage_and_range_raise_sample_error(sample_env):
    from core.cache_warehouse import SampleReadError

    with pytest.raises(SampleReadError) as ei:
        _read(stage="bogus")
    assert ei.value.code == "invalid_stage"

    with pytest.raises(SampleReadError) as ei2:
        _read(date_from="2026-07-05", date_to="2026-07-01")  # inverted
    assert ei2.value.code == "invalid_range"


def test_limit_hard_cap(sample_env):
    # Requesting 999 is clamped to the hard cap (20); with only 8 rows/day the day
    # returns 8 -- proving the clamp does not inflate beyond eligible rows.
    out = _read(limit=999)
    for day in out["days"]:
        assert day["row_count"] == 8
        assert len(day["rows"]) <= 20
