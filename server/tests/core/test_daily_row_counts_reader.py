"""`cache_warehouse.read_daily_row_counts`, actually executed -- story 58.1.

WHY THIS FILE EXISTS. The reader is ~100 lines across two SQL dialects and, until
this file, every one of its call sites in the suite was a `patch(...)`. The route's
`days[].rows` was therefore asserted against a fixture's opinion of the reader and
never against the reader: a `GROUP BY` that returned the wrong key, a probe that
answered truthy on an empty relation, or an isolation filter left off the statement
would all have shipped green.

DRIVEN AGAINST A REAL DUCKDB MART, on the pattern of
`tests/core/test_datastream_sample_reader.py` -- a temp origin playing
`main_marts.*`, no Postgres. **The BigQuery branch is NOT exercised here and that
is stated rather than implied**: it needs a GCP dataset and credentials, this
repository has no connector test accounts, and a mocked BigQuery client would prove
the test can write BigQuery SQL and nothing else. What holds the two dialects in
step is that they answer the same shape from the same function; that shape is what
is pinned below.

AND THE LAST TEST EXECUTES THE ROUTE'S OWN COMPOSITION over the real reader, so the
path from a mart row to `days[].rows` is proven end to end rather than in halves.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

_FACT_DDL = """
    CREATE TABLE main_marts.fact_daily_kpi (
        project_id          TEXT,
        date                DATE,
        connector           TEXT,
        metric              TEXT,
        breakdown_dimension TEXT,
        breakdown_value     TEXT,
        value               DOUBLE,
        pull_id             TEXT,
        loaded_at           TIMESTAMP
    )
"""

#: Deliberately uneven, so a count that ignored its `GROUP BY` and answered a
#: single total would be visible: 3 rows, then 1, then a day with none, then 2.
_ROWS_PER_DAY = {0: 3, 1: 1, 2: 0, 3: 2}
_BASE = date(2026, 7, 1)


def _seed(path: str, *, empty: bool = False) -> None:
    import duckdb

    con = duckdb.connect(path)
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
        con.execute(_FACT_DDL)
        if empty:
            return
        rows = []
        for offset, count in _ROWS_PER_DAY.items():
            day = _BASE + timedelta(days=offset)
            for i in range(count):
                rows.append(
                    ("p1", day, "meta-ads", f"m{i}", "device_category", "mobile",
                     float(i), f"pull_{offset}_{i}", None)
                )
        # AD-5 and connector isolation: a row of another project and a row of
        # another connector, both on a day inside the window. Either one leaking
        # would inflate a day this Datastream is being asked about.
        rows.append(("p2", _BASE, "meta-ads", "m0", "d", "v", 1.0, "pull_other_p", None))
        rows.append(("p1", _BASE, "google-analytics", "m0", "d", "v", 1.0,
                     "pull_other_c", None))
        # And a row OUTSIDE the window, so the date bounds have something to exclude.
        rows.append(("p1", _BASE + timedelta(days=40), "meta-ads", "m0", "d", "v",
                     1.0, "pull_far", None))
        con.executemany(
            "INSERT INTO main_marts.fact_daily_kpi VALUES (?,?,?,?,?,?,?,?,?)", rows
        )
    finally:
        con.close()


@pytest.fixture
def mart(tmp_path, monkeypatch):
    origin = str(tmp_path / "origin_local.duckdb")
    _seed(origin)
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", origin)
    monkeypatch.setenv("TOOROW_CACHE_ENABLED", "false")
    return origin


@pytest.fixture
def empty_mart(tmp_path, monkeypatch):
    origin = str(tmp_path / "origin_empty.duckdb")
    _seed(origin, empty=True)
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", origin)
    monkeypatch.setenv("TOOROW_CACHE_ENABLED", "false")
    return origin


def _count(**over):
    from core.cache_warehouse import read_daily_row_counts

    kwargs = dict(
        project_id="p1",
        connector="meta-ads",
        date_from="2026-07-01",
        date_to="2026-07-04",
    )
    kwargs.update(over)
    return read_daily_row_counts(**kwargs)


# ---------------------------------------------------------------------------
# What it counts.
# ---------------------------------------------------------------------------


def test_each_day_is_counted_on_its_own(mart) -> None:
    out = _count()
    assert out["connector_present"] is True
    assert out["counts"] == {"2026-07-01": 3, "2026-07-02": 1, "2026-07-04": 2}


def test_a_day_with_no_mart_row_is_absent_rather_than_invented(mart) -> None:
    """The reader reports what it counted; the CALLER decides what an absence means.

    A `0` written here would be indistinguishable from a connector the mart does
    not model, which is the distinction the whole reader exists to keep.
    """
    assert "2026-07-03" not in _count()["counts"]


def test_the_date_bounds_exclude_what_is_outside_the_window(mart) -> None:
    out = _count(date_from="2026-07-01", date_to="2026-07-02")
    assert out["counts"] == {"2026-07-01": 3, "2026-07-02": 1}


def test_another_projects_rows_are_never_counted(mart) -> None:
    """AD-5. `p2` has a row on 2026-07-01; `p1` must still read 3, never 4."""
    assert _count()["counts"]["2026-07-01"] == 3
    assert _count(project_id="p2")["counts"] == {"2026-07-01": 1}


def test_another_connectors_rows_are_never_counted(mart) -> None:
    assert _count()["counts"]["2026-07-01"] == 3
    assert _count(connector="google-analytics")["counts"] == {"2026-07-01": 1}


# ---------------------------------------------------------------------------
# What it refuses to guess.
# ---------------------------------------------------------------------------


def test_a_connector_absent_from_this_projects_mart_is_reported_absent(mart) -> None:
    """23 of the 39 connectors never reach `fact_daily_kpi`.

    Without this probe, every day of those Datastreams would count `0` -- a
    measurement nobody made, and one a screen would render as "collected nothing".
    """
    out = _count(connector="klaviyo")
    assert out["connector_present"] is False
    assert out["counts"] == {}


def test_an_absent_relation_is_reported_absent_and_not_as_a_failure(mart, tmp_path,
                                                                   monkeypatch) -> None:
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "no_such.duckdb"))
    out = _count()
    assert out == {"connector_present": False, "counts": {}}


def test_an_empty_relation_is_present_but_counts_nothing(empty_mart) -> None:
    """The mart exists, this connector has never landed in it: still an absence."""
    out = _count()
    assert out["connector_present"] is False
    assert out["counts"] == {}


def test_a_datastream_with_no_connector_asks_the_warehouse_nothing(mart) -> None:
    assert _count(connector="") == {"connector_present": False, "counts": {}}


def test_the_window_bound_is_enforced_by_the_reader_itself(mart) -> None:
    from core.cache_warehouse import SAMPLE_MAX_DAYS, SampleReadError

    with pytest.raises(SampleReadError) as refused:
        _count(date_from="2026-01-01", date_to="2026-12-31")
    assert refused.value.code == "invalid_range"
    assert str(SAMPLE_MAX_DAYS) in str(refused.value)


def test_an_inverted_window_is_refused_rather_than_silently_emptied(mart) -> None:
    from core.cache_warehouse import SampleReadError

    with pytest.raises(SampleReadError):
        _count(date_from="2026-07-04", date_to="2026-07-01")


def test_an_unknown_backend_raises_the_typed_error_and_never_a_raw_one(
    mart, monkeypatch
) -> None:
    from core.cache_warehouse import SampleReadError

    monkeypatch.setenv("TOOROW_DB_MODE", "postgres_but_not_really")
    with pytest.raises(SampleReadError) as refused:
        _count()
    assert refused.value.code == "warehouse_unavailable"


# ---------------------------------------------------------------------------
# What it costs.
# ---------------------------------------------------------------------------


def test_the_whole_window_costs_two_statements_and_never_one_per_day(mart) -> None:
    """The defect this reader was written to avoid.

    `read_datastream_sample` beside it runs `for day in days:` and issues one
    warehouse query per day -- 92 at its ceiling. A COUNT does not need that: the
    `GROUP BY` answers the strip at once. The second statement is the presence
    probe, and it is bounded by `LIMIT 1`.
    """
    from core import warehouse

    executed: list[str] = []
    real = warehouse._query_duckdb

    def _spy(sql, params):
        executed.append(" ".join(str(sql).split()))
        return real(sql, params)

    warehouse._query_duckdb = _spy
    try:
        out = _count(date_from="2026-07-01", date_to="2026-07-04")
    finally:
        warehouse._query_duckdb = real

    assert out["counts"]  # it really read something
    assert len(executed) == 2, executed
    assert sum("GROUP BY" in sql.upper() for sql in executed) == 1, executed
    assert sum("LIMIT 1" in sql.upper() for sql in executed) == 1, executed


# ---------------------------------------------------------------------------
# And the route's own composition, over the real reader.
# ---------------------------------------------------------------------------


def test_a_mart_row_reaches_the_payload_as_a_days_rows(mart) -> None:
    """End to end over the REAL warehouse: no `patch` on the reader anywhere here."""
    from core.datastream_daily_breakdown_api import read_daily_breakdown

    from tests.core.test_datastream_daily_breakdown import _conn, _pull

    payload = read_daily_breakdown(
        _conn(pulls=[_pull(date_from="2026-07-01", date_to="2026-07-01")]),
        project_id="p1",
        datastream_id="ds_001",
        start="2026-07-01",
        end="2026-07-04",
        span=4,
        view_mode="processed",
    )

    rows_by_day = {day["date"]: day["rows"] for day in payload["days"]}
    assert rows_by_day == {
        "2026-07-01": 3,
        "2026-07-02": 1,
        # The mart HAS this connector, so a day it holds no row for really is a
        # zero -- and that is the only case in which this route publishes one.
        "2026-07-03": 0,
        "2026-07-04": 2,
    }
    assert all(day["rows_reason"] is None for day in payload["days"])


def test_a_connector_outside_the_mart_publishes_an_absence_through_the_route(
    mart,
) -> None:
    """The same path, with the connector the mart does not model: never a zero."""
    from core.datastream_daily_breakdown_api import (
        ROWS_CONNECTOR_NOT_IN_MART,
        read_daily_breakdown,
    )

    from tests.core.test_datastream_daily_breakdown import _conn, _pull

    payload = read_daily_breakdown(
        _conn(pulls=[_pull(date_from="2026-07-01", date_to="2026-07-01")],
              facts=[("klaviyo", "dsp_1", "dmap_1", "campaign_daily")]),
        project_id="p1",
        datastream_id="ds_001",
        start="2026-07-01",
        end="2026-07-04",
        span=4,
        view_mode="processed",
    )

    assert payload["connector"] == "klaviyo"
    assert all(day["rows"] is None for day in payload["days"])
    assert all(
        day["rows_reason"] == ROWS_CONNECTOR_NOT_IN_MART for day in payload["days"]
    )
