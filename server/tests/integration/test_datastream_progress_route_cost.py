"""What one poll of the progress route costs, counted -- 63.2 acceptance, AI-219.

WHY THIS FILE EXISTS. 63.2 bought a route to replace a nine-query, two-connection
`overview` call, and 63.3 put it on a five-second timer across three surfaces.
That acceptance is a NUMBER, and until now nothing recomputed it: AI-219 added a
membership proof to the shared role guard and would have taken the tick from two
statements to three, silently, on the one route in the product designed to be
called in a loop.

WHAT IS COUNTED. Connections opened and statements executed, at the real ASGI
seal, against a real database -- not a fake cursor, which would count whatever
the fixture felt like answering. The route is driven twice: the tick that repeats
while a run moves, and the tick that ends the poll.
"""

from __future__ import annotations

import os
import uuid

import pytest
from starlette.testclient import TestClient

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

_skip_without_dsn = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live cost measurement skipped",
)

#: THE KEY THE DOOR REALLY HANDS DOWN, and the reason this file counts three and
#: not four (measured 2026-08-31). The stub below replaces `_check_auth`, so the
#: identity it returns is this file's decision, and it used to be a raw e-mail --
#: `poller@example.com`. `project_access` then paid
#: `SELECT person_id FROM app.person_identities WHERE subject = %s LIMIT 2` to
#: translate it, and that fourth statement was the file's own stub, not the route.
#:
#: In the deployed path `resolve_request_principal` returns `principal.person_id`
#: (`api_auth._authenticate_canonical_api_request`), and `identity_bridge`
#: property 2 -- "what is already canonical does not move" -- returns a
#: `person_`-prefixed identity WITHOUT a read. So the per-request authorization
#: read costs nothing on this route, the ceiling stays three, and a stub that
#: hands anything but a canonical person measures the stub.
#:
#: README.md 'One authorization key' is what makes this the verdict rather than a
#: preference: the person is "the only key any authorization decision may be
#: taken on", so a raw subject is not an identity this route can be polled with.
IDENTITY = "person_01J8ZC4Q0N7R2K3W5X6Y7Z8A9B"


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _dse() -> str:
    """A house-style execution id: `ck_datastream_executions_id` demands the shape."""
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    body = uuid.uuid4().int
    chars = []
    for _ in range(26):
        body, index = divmod(body, len(alphabet))
        chars.append(alphabet[index])
    return "dse_" + "".join(reversed(chars))


def _digest(*parts: str) -> str:
    """64 lowercase hex characters -- the shape every `*_hash` CHECK requires."""
    import hashlib

    return hashlib.sha256("|".join(parts).encode()).hexdigest()


class _CountingCursor:
    """A real cursor that reports every statement it is given."""

    def __init__(self, cursor, ledger: list[str]) -> None:
        self._cursor = cursor
        self._ledger = ledger

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, *exc):
        return self._cursor.__exit__(*exc)

    def execute(self, query, params=None, *args, **kwargs):
        self._ledger.append(" ".join(str(query).split())[:90])
        return self._cursor.execute(query, params, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _CountingConnection:
    def __init__(self, connection, ledger: list[str]) -> None:
        self._connection = connection
        self._ledger = ledger

    def cursor(self, *args, **kwargs):
        return _CountingCursor(self._connection.cursor(*args, **kwargs), self._ledger)

    def __getattr__(self, name):
        return getattr(self._connection, name)


@pytest.fixture
def a_running_flux(live_postgres, monkeypatch):
    """One org, one member, one project, one Datastream, one run in flight."""
    monkeypatch.setenv("TOOROW_AUTH_MODE", "static")
    # `build_asgi_app()` refuses `static` without a token, so the file supplied
    # half its own precondition and failed unless the operator happened to export
    # the other half. A test that measures the shell's environment is not
    # measuring the route.
    monkeypatch.setenv("TOOROW_STATIC_TOKEN", "route-cost-local-token")
    # AND THE SAME CLASS AGAIN (2026-08-31): `core.main` refuses to import under
    # any non-`disabled` auth mode without this secret, so the file passed only
    # when some earlier test in the same process had already imported `core.main`
    # under `disabled`. Run alone it died on the import, not on a count -- an
    # instrument whose verdict depends on what ran before it measures nothing.
    monkeypatch.setenv(
        "TOOROW_FEEDBACK_CONTEXT_SECRET", "route-cost-local-feedback-secret-32bytes+"
    )
    conn = live_postgres
    org_id, project_id, ds_id = _id("org_"), _id("proj_"), _id("ds_")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by, status)"
            " VALUES (%s, %s, %s, 'ai-219-cost', 'active')",
            (org_id, org_id, org_id),
        )
        cur.execute(
            "INSERT INTO app.org_members (id, org_id, identity, role, status)"
            " VALUES (%s, %s, %s, 'owner', 'active')",
            (_id("mem_"), org_id, IDENTITY),
        )
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, created_by, org_id, status)"
            " VALUES (%s, %s, %s, 'ai-219-cost', %s, 'active')",
            (project_id, project_id, project_id, org_id),
        )
        cur.execute(
            "INSERT INTO app.datastreams (id, project_id, name, module_name,"
            " source_kind, enabled, created_by, org_id)"
            " VALUES (%s, %s, 'DS', 'generic', 'connector_pull', FALSE,"
            " 'ai-219-cost', %s)",
            (ds_id, project_id, org_id),
        )
    try:
        yield conn, project_id, ds_id
    finally:
        conn.rollback()


def _poll(conn, project_id: str, ds_id: str) -> tuple[list[str], int, dict]:
    """Drive the route through the real app; return (statements, connections, body)."""
    from unittest.mock import AsyncMock, patch

    from core.main import build_asgi_app

    ledger: list[str] = []
    connections = 0

    class _Once:
        def __enter__(self_inner):
            nonlocal connections
            connections += 1
            return _CountingConnection(conn, ledger)

        def __exit__(self_inner, *exc):
            return False

    with patch(
        "core.admin_api._check_auth", new=AsyncMock(return_value=(True, IDENTITY))
    ), patch("core.db.get_connection", side_effect=lambda *a, **k: _Once()):
        client = TestClient(build_asgi_app(), raise_server_exceptions=False)
        response = client.get(
            f"/api/projects/{project_id}/datastreams/{ds_id}/progress"
        )
    assert response.status_code == 200, response.text
    return ledger, connections, response.json()


@_skip_without_dsn
def test_one_poll_of_an_idle_flux_costs_one_connection_and_three_statements(
    a_running_flux, capsys
) -> None:
    """The measured ceiling: guard, progress read, and the read that ENDS the poll.

    `idle` is the branch that stops the polling, so its statement is paid once
    per run and not once per tick. The tick that repeats is two statements.
    """
    conn, project_id, ds_id = a_running_flux

    statements, connections, _body = _poll(conn, project_id, ds_id)

    with capsys.disabled():
        print()
        print(f"  connections opened : {connections}")
        print(f"  statements executed: {len(statements)}")
        for statement in statements:
            print(f"    {statement}")

    assert connections == 1, statements
    assert len(statements) == 3, statements


def _seed_run(conn, project_id: str, ds_id: str, state: str, version: int) -> str:
    """One execution of this Datastream, with the two versions it may not be without.

    Everything stays inside the fixture's transaction, which is rolled back: the
    immutability triggers on the version tables are never asked to forgive a
    delete, because nothing is ever committed.
    """
    plan_id, mapping_id, exec_id = _id("dsp_"), _id("dmap_"), _dse()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.datastream_plan_versions"
            " (id, datastream_id, project_id, version_number, contract_version,"
            "  source_kind, writer_kind, destination_policy, normalized_payload,"
            "  content_hash, idempotency_key_hash, created_by)"
            " VALUES (%s, %s, %s, %s, '1', 'connector_pull', 'toorow', 'managed_raw',"
            "         '{}'::jsonb, %s, %s, 'ai-219-cost')",
            (plan_id, ds_id, project_id, version, _digest(plan_id), _digest(plan_id, "k")),
        )
        cur.execute(
            "INSERT INTO app.datastream_mapping_versions"
            " (id, datastream_id, project_id, version_number, mapping_contract_version,"
            "  source_schema_hash, plan_version_id, content_hash, ossie_spec_version,"
            "  toorow_extension_version, executable, mapping_payload, ossie_projection,"
            "  idempotency_key_hash, created_by)"
            " VALUES (%s, %s, %s, %s, '1', %s, %s, %s, '0.1.1', '1', TRUE,"
            "         '{}'::jsonb, '{}'::jsonb, %s, 'ai-219-cost')",
            (mapping_id, ds_id, project_id, version, _digest(mapping_id, "s"), plan_id,
             _digest(mapping_id), _digest(mapping_id, "k")),
        )
        cur.execute(
            "INSERT INTO app.datastream_executions"
            " (id, datastream_id, project_id, plan_version_id, mapping_version_id,"
            "  projection_plan_ref, state, created_by)"
            " VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, %s, 'ai-219-cost')",
            (exec_id, ds_id, project_id, plan_id, mapping_id, state),
        )
    return exec_id


def _seed_window(conn, ids, exec_id, date_from, date_to, state, started_ago,
                 completed_ago=None):
    """One pull job of that run, with the only per-window chronometer there is.

    Both instants come from the DATABASE, as intervals off `now()`: a timestamp
    this test computed with its own clock would be the very mixture story 63.4
    refuses. `completed_ago=None` is a window still in flight.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.pull_jobs"
            " (id, pull_id, connection_ref_id, date_from, date_to, state,"
            "  requested_by, datastream_id, execution_id, started_at, completed_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, 'ai-219-cost', %s, %s,"
            "         now() - make_interval(secs => %s),"
            "         CASE WHEN %s::float IS NULL THEN NULL"
            "              ELSE now() - make_interval(secs => %s::float) END)",
            (
                _id("job_"), _id("pull_"), ids["connection_ref_id"], date_from, date_to,
                state, ids["ds_id"], exec_id, started_ago, completed_ago, completed_ago,
            ),
        )


@_skip_without_dsn
def test_the_estimate_is_measured_on_real_rows_and_costs_no_extra_statement(
    a_running_flux, capsys
) -> None:
    """Story 63.4, at the seal: three finished runs, a median, and TWO statements.

    A fake cursor cannot prove that the history LATERAL aggregates what it claims
    -- it answers whatever the fixture felt like. Here the rates come out of
    Postgres.

    EACH PAST RUN HAS A FAILED WINDOW IN THE MIDDLE OF IT, and that is the point.
    Two collected windows of 5 days costing 300 s each, and between them a
    `dead_letter` window that burned an hour. The rate of the run is
    (300 + 300) / (5 + 5) = 60 seconds per day. Summing the run's SPAN instead --
    `max(completed_at) - min(started_at)` over its collected windows -- charges
    the failed window's hour to days that were never collected and answers 390,
    a factor of 6.5, on a run where nothing about the collection changed.

    This is also the first time the repeating tick itself is counted: the earlier
    measurement drove a flux with NO run in flight, which is the branch that pays
    for the `idle` read and ends the poll.
    """
    conn, project_id, ds_id = a_running_flux
    with conn.cursor() as cur:
        cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
        org_id = cur.fetchone()[0]
        connection_ref_id = _id("cref_")
        cur.execute(
            "INSERT INTO app.connection_ref"
            " (id, provider, nango_connection_id, project_id, status, enabled,"
            "  owner_org_id, owner_identity)"
            " VALUES (%s, 'generic', %s, %s, 'active', TRUE, %s, %s)",
            (connection_ref_id, connection_ref_id, project_id, org_id, IDENTITY),
        )
    ids = {"ds_id": ds_id, "connection_ref_id": connection_ref_id}

    for index in range(3):
        past = _seed_run(conn, project_id, ds_id, "collected", index + 1)
        base = 86_400 * (index + 1)
        _seed_window(conn, ids, past, "2026-07-01", "2026-07-05", "done",
                     base, base - 300)
        # An hour of wall time that collected nothing. Its days are not in the
        # denominator, so its seconds may not be in the numerator.
        _seed_window(conn, ids, past, "2026-07-06", "2026-07-10", "dead_letter",
                     base - 300, base - 3900)
        _seed_window(conn, ids, past, "2026-07-11", "2026-07-15", "done",
                     base - 3900, base - 4200)

    # And the run in flight: 20 days declared, 10 done, a 10-day window a minute
    # in. `uq_datastream_executions_active` allows exactly one of these.
    running = _seed_run(conn, project_id, ds_id, "loading", 4)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.datastream_executions SET days_total = 20, days_done = 10,"
            " started_at = now() - make_interval(secs => 60) WHERE id = %s",
            (running,),
        )
    _seed_window(conn, ids, running, "2026-08-01", "2026-08-10", "queued", 60)

    statements, connections, payload = _poll(conn, project_id, ds_id)
    estimate = payload["progress"]["estimate"]

    with capsys.disabled():
        print()
        print(f"  connections opened          : {connections}")
        print(f"  statements for a MOVING run : {len(statements)}")
        print(f"  estimate                    : {estimate}")

    # The tick that REPEATS: the guard and the read, and nothing else.
    assert connections == 1, statements
    assert len(statements) == 2, statements

    assert estimate["armed"] is True
    assert estimate["observations"] == 3
    assert estimate["minimum_observations"] == 3
    # Three runs that agreed exactly: a point, and a spread of 1.
    assert estimate["precision"] == "point"
    assert estimate["spread_ratio"] == 1.0
    # 10 days owed at 60 s/day = 600 s, less the ~60 s the window has run. The
    # run-span arithmetic would answer about 3 900 here.
    assert 500 <= estimate["seconds_remaining"] <= 600, estimate
    assert estimate["behind_by_seconds"] == 0
    assert estimate["sentence"].startswith("About "), estimate
    # The per-window chronometer reached the payload, which is what lets the
    # estimate see a window that has outrun what this Datastream usually spends.
    assert payload["progress"]["window_in_progress"]["started_at"] is not None
    assert payload["progress"]["window_in_progress"]["completed_at"] is None


@_skip_without_dsn
def test_the_guard_does_not_pay_for_a_pair_the_read_already_carries(
    a_running_flux,
) -> None:
    """AI-219's proof must be FREE here: `PROGRESS_SQL` already joins on the pair.

    A membership statement of its own would be a pure cost on the one route in
    the product that runs on a timer -- and it would prove exactly what
    `d.id = %s AND d.project_id = %s` proves one statement later. The guard is
    told, at the call site, that the read carries the pair; the conformance sweep
    checks that claim against the reader's real SQL rather than trusting it.
    """
    conn, project_id, ds_id = a_running_flux

    statements, _connections, _body = _poll(conn, project_id, ds_id)

    membership = [s for s in statements if "FROM app.datastreams WHERE id" in s]
    assert not membership, (
        "the shared guard ran its own membership statement on the polled route:\n  "
        + "\n  ".join(statements)
    )
