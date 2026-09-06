"""What one day-grain read costs, counted -- story 58.1 acceptance, arbitrage 10.

WHY THIS FILE EXISTS. Two costs grow silently on a route like this one, and
neither shows up in a functional test:

  * ONE STATEMENT PER DAY. The bounded sample beside this route does exactly
    that -- `cache_warehouse.read_datastream_sample` runs `for day in days:` and
    issues 92 warehouse queries at its ceiling. Inheriting that shape here would
    have made the cheapest question of the surface its most expensive answer. So
    the statement count is measured against a ONE-day window and a NINETY-TWO-day
    window, and it has to be the same number.

  * ONE HEAP FETCH PER PULL JOB EVER MADE. The statement count does NOT move
    when that happens, which is precisely why migration 219 had to be written
    down rather than noticed. The ledger's overlap test had both of its date
    bounds evaluated as a heap `Filter`, so every read of a strip of days
    re-fetched every window the Datastream ever had. Migration 221 gives them an
    index to be a CONDITION in, and this file holds the index's shape plus the
    plan of the statement the route actually executed -- not one the test
    retyped, which would prove only that the test can write SQL.

AND IT NEVER COMMITS. An earlier version of this file committed its 730 windows
so that `ANALYZE` could see them and the planner would pick the new index; the
teardown then had to unpick a project through nine foreign keys and left rows
behind. The fixture rolls back, and the two assertions below are the ones that
survive being unable to bias the planner -- which is the honest set.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

_skip_without_dsn = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live cost measurement skipped",
)

IDENTITY = "reader@example.com"
AUTHOR = "story-58-1-cost"

#: The index migration 221 creates, and the one this read must land on.
WINDOW_INDEX = "idx_pull_jobs_datastream_dates"


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


class _CountingCursor:
    """A real cursor that reports every statement it is given, with its params."""

    def __init__(self, cursor, ledger: list[tuple]) -> None:
        self._cursor = cursor
        self._ledger = ledger

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, *exc):
        return self._cursor.__exit__(*exc)

    def execute(self, query, params=None, *args, **kwargs):
        self._ledger.append((" ".join(str(query).split()), params))
        return self._cursor.execute(query, params, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _CountingConnection:
    def __init__(self, connection, ledger: list[tuple]) -> None:
        self._connection = connection
        self._ledger = ledger

    def cursor(self, *args, **kwargs):
        return _CountingCursor(self._connection.cursor(*args, **kwargs), self._ledger)

    def __getattr__(self, name):
        return getattr(self._connection, name)


@pytest.fixture
def a_stream_with_history(live_postgres, monkeypatch):
    """One Datastream and TWO YEARS of nightly windows -- 730 rows on `pull_jobs`.

    The volume is the point: it is what a stream that has collected every night
    since it was created actually holds, and it is the only condition under which
    a heap filter on the date bounds is visible at all.
    """
    monkeypatch.setenv("TOOROW_AUTH_MODE", "static")
    monkeypatch.setenv("TOOROW_STATIC_TOKEN", "daily-breakdown-cost-token")
    conn = live_postgres
    org_id, project_id = _id("org_"), _id("proj_")
    ds_id, connection_ref_id = _id("ds_"), _id("cref_")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by, status)"
            " VALUES (%s, %s, %s, %s, 'active')",
            (org_id, org_id, org_id, AUTHOR),
        )
        cur.execute(
            "INSERT INTO app.org_members (id, org_id, identity, role, status)"
            " VALUES (%s, %s, %s, 'owner', 'active')",
            (_id("mem_"), org_id, IDENTITY),
        )
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, created_by, org_id, status)"
            " VALUES (%s, %s, %s, %s, %s, 'active')",
            (project_id, project_id, project_id, AUTHOR, org_id),
        )
        cur.execute(
            "INSERT INTO app.connection_ref"
            " (id, provider, nango_connection_id, project_id, status, enabled,"
            "  owner_org_id, owner_identity)"
            " VALUES (%s, 'meta-ads', %s, %s, 'active', TRUE, %s, %s)",
            (connection_ref_id, connection_ref_id, project_id, org_id, IDENTITY),
        )
        cur.execute(
            "INSERT INTO app.datastreams (id, project_id, name, module_name,"
            " source_kind, connection_ref_id, enabled, created_by, org_id)"
            " VALUES (%s, %s, 'Stream', 'meta-ads', 'connector_pull', %s, TRUE, %s, %s)",
            (ds_id, project_id, connection_ref_id, AUTHOR, org_id),
        )
        # 730 nightly windows, generated in ONE statement: a per-row insert loop
        # would make the fixture slower than the thing it measures.
        cur.execute(
            "INSERT INTO app.pull_jobs"
            " (id, pull_id, datastream_id, connection_ref_id, date_from, date_to,"
            "  state, requested_by, completed_at)"
            " SELECT %s || n, %s || n, %s, %s,"
            "        (DATE '2026-06-16' - n), (DATE '2026-06-16' - n),"
            "        'done', %s, now()"
            " FROM generate_series(0, 729) AS n",
            (_id("job_"), _id("pull_"), ds_id, connection_ref_id, AUTHOR),
        )
    try:
        yield {
            "conn": conn,
            "project_id": project_id,
            "ds_id": ds_id,
        }
    finally:
        conn.rollback()


def _read(ids, *, start: str, end: str) -> tuple[list[tuple], int, dict]:
    """Drive the route through the real app; return (statements, connections, body)."""
    from core.main import build_asgi_app

    ledger: list[tuple] = []
    connections = 0
    connection = ids["conn"]

    @contextmanager
    def _open(*_a, **_k):
        nonlocal connections
        connections += 1
        yield _CountingConnection(connection, ledger)

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, IDENTITY))),
        patch("core.db.get_connection", side_effect=_open),
        patch(
            "core.cache_warehouse.read_daily_row_counts",
            return_value={"connector_present": False, "counts": {}},
        ),
    ):
        client = TestClient(build_asgi_app(), raise_server_exceptions=False)
        response = client.get(
            f"/api/projects/{ids['project_id']}/datastreams/{ids['ds_id']}"
            "/daily-breakdown",
            params={"start": start, "end": end},
        )
    assert response.status_code == 200, response.text
    return ledger, connections, response.json()


@_skip_without_dsn
def test_a_ninety_two_day_window_costs_what_a_one_day_window_costs(
    a_stream_with_history, capsys
) -> None:
    """No statement per day. The bounded sample beside this route issues 92.

    A read whose cost is proportional to the width of the window cannot be the
    default view of a tab, and nothing but a count catches it: the payload of a
    92-query version is byte-identical to the payload of this one.
    """
    from core.datastream_daily_breakdown_api import MAX_WINDOW_DAYS

    one_day, _c1, narrow = _read(
        a_stream_with_history, start="2026-06-16", end="2026-06-16"
    )
    ninety_two, connections, wide = _read(
        a_stream_with_history, start="2026-03-17", end="2026-06-16"
    )

    with capsys.disabled():
        print()
        print(f"  connections opened            : {connections}")
        print(f"  statements for   1 day        : {len(one_day)}")
        print(f"  statements for {MAX_WINDOW_DAYS} days       : {len(ninety_two)}")
        for statement, _params in ninety_two:
            print(f"    {statement[:100]}")

    assert len(narrow["days"]) == 1
    assert len(wide["days"]) == MAX_WINDOW_DAYS
    assert connections == 1, ninety_two
    assert len(ninety_two) == len(one_day), (
        "the day-grain read costs more statements for a wider window -- it has "
        "grown a per-day query:\n  "
        + "\n  ".join(statement for statement, _ in ninety_two)
    )


@_skip_without_dsn
def test_the_cost_does_not_grow_with_the_history_of_the_stream(
    a_stream_with_history, capsys
) -> None:
    """730 windows in the table, 92 in the answer: the count may not follow the table."""
    before, _c, _b = _read(a_stream_with_history, start="2026-03-17", end="2026-06-16")

    with a_stream_with_history["conn"].cursor() as cur:
        cur.execute(
            "INSERT INTO app.pull_jobs"
            " (id, pull_id, datastream_id, connection_ref_id, date_from, date_to,"
            "  state, requested_by, completed_at)"
            " SELECT %s || n, %s || n, %s,"
            "        (SELECT connection_ref_id FROM app.datastreams WHERE id = %s),"
            "        (DATE '2024-06-16' - n), (DATE '2024-06-16' - n),"
            "        'done', %s, now()"
            " FROM generate_series(0, 729) AS n",
            (_id("job_"), _id("pull_"), a_stream_with_history["ds_id"],
             a_stream_with_history["ds_id"], AUTHOR),
        )

    after, _c2, _b2 = _read(a_stream_with_history, start="2026-03-17", end="2026-06-16")

    with capsys.disabled():
        print()
        print(f"  statements with  730 windows : {len(before)}")
        print(f"  statements with 1460 windows : {len(after)}")

    assert len(after) == len(before)


@_skip_without_dsn
def test_the_window_index_exists_and_carries_both_date_bounds(
    a_stream_with_history, capsys
) -> None:
    """Migration 221, asserted on the artefact and not on the planner's mood.

    WHY THE INDEX NAME IS NOT PINNED IN A PLAN. Measured 2026-08-06 on the
    disposable cluster, with 730 committed windows and a fresh `ANALYZE`:

        Index Scan using idx_pull_jobs_datastream_dates   cost=0.28..8.30
        Index Scan using idx_pull_jobs_datastream_id      cost=0.27..8.29

    One hundredth of a unit apart. Which one wins is decided by whatever
    `pg_statistic` last saw, so a test that pinned the winner would pin planner
    noise and go red on a table nobody touched. What migration 221 actually
    bought is a STRUCTURE: an index whose leading column is the stream and whose
    next two are exactly the columns the ledger's overlap test filters on, so
    both bounds CAN be an index condition instead of a heap filter. That is a
    fact about the index, it is true whatever the planner decides today, and it
    is what regresses if somebody drops the migration.
    """
    with a_stream_with_history["conn"].cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes"
            " WHERE schemaname = 'app' AND tablename = 'pull_jobs'"
            "   AND indexname = %s",
            (WINDOW_INDEX,),
        )
        row = cur.fetchone()

    with capsys.disabled():
        print()
        print(f"  {row[0] if row else '<absent>'}")

    assert row is not None, (
        f"{WINDOW_INDEX} is absent: the ledger's two date bounds are back to "
        "being a heap filter, and every read of a strip of days re-fetches every "
        "window the Datastream ever had (migration 221)"
    )
    definition = " ".join(row[0].split())
    assert "(datastream_id, date_from, date_to)" in definition, definition
    # No partial predicate: this read asks about every window of the stream,
    # whatever state it ended in.
    assert " WHERE " not in definition.upper(), definition


@_skip_without_dsn
def test_the_ledger_never_reads_the_pull_jobs_table_sequentially(
    a_stream_with_history, capsys
) -> None:
    """The plan is taken of the statement the route ACTUALLY ran.

    Retyping the SQL here would prove the test can write SQL and nothing about
    the route. What is asserted is the one thing that is not planner noise: this
    table is never read end to end to answer about one stream.
    """
    statements, _connections, _body = _read(
        a_stream_with_history, start="2026-03-17", end="2026-06-16"
    )
    overlap = [
        (query, params)
        for query, params in statements
        if "FROM app.pull_jobs pj" in query
    ]
    assert len(overlap) == 1, [q for q, _ in statements]
    query, params = overlap[0]

    with a_stream_with_history["conn"].cursor() as cur:
        cur.execute(f"EXPLAIN (COSTS OFF) {query}", params)
        plan = "\n".join(row[0] for row in cur.fetchall())

    with capsys.disabled():
        print()
        for line in plan.splitlines():
            print(f"  {line}")

    assert "Seq Scan on pull_jobs" not in plan, plan
    assert "pull_jobs" in plan, plan
