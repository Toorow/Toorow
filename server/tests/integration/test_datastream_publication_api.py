"""Real-ASGI seams for the atomic publication routes (Story 12.5, AI-56).

Through build_asgi_app(): Member creates an execution (201); Viewer is read-only
(404 on mutation routes); cross-project / zero-membership returns a non-disclosing
404. The DB is mocked at the core.db.get_connection boundary and the
publication-module functions are patched so these run WITHOUT Postgres (the live
transaction is proven in the pg-gated constraints test). Kept deliberately small
(AI-60).

AI-126: the direct publish route (POST /api/datastreams/{id}/executions/
{exec_id}/publish) was RETIRED by 26695dcc (six-tab Workbench) -- ungoverned
publication is closed; publishing goes through the governed confirmation flow
(core.governed_publication). Its handler `_publish_datastream_execution` has
been deleted. The three behaviour tests that exercised it are kept as
strict-xfail (the retirement must stay loud: a remount is an UNEXPECTEDLY
PASSING failure), and the former role-guard test is rewritten to prove the
route's ABSENCE for real -- it used to pass BECAUSE the route was missing, a
404 satisfying its assertion as well as a legitimate refusal.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core.main import build_asgi_app  # noqa: E402


def _db_context():
    conn = MagicMock()
    cursor = MagicMock()
    # The strict Datastream guard proves the route's id/project pair before the
    # publication seam (mocked below) is reached.
    cursor.fetchone.return_value = (1,)
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cursor)
    context = MagicMock()
    context.__enter__ = MagicMock(return_value=conn)
    context.__exit__ = MagicMock(return_value=False)
    return context


def _client(role_allowed=True):
    app = build_asgi_app()
    return (
        TestClient(app, raise_server_exceptions=True),
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "user-1"))),
        patch("core.db.get_connection", return_value=_db_context()),
        patch("core.project_access.identity_has_project_role", return_value=role_allowed),
    )


_EXEC = {
    "id": "dse_01J8ZC4Q0N7R2K3W5X6Y7Z8A9B",
    "datastream_id": "ds_01",
    "project_id": "proj_a",
    "state": "created",
}

_PLAN = {"executable": True, "issues": []}


def _create_body(**overrides):
    body = {
        "project_id": "proj_a",
        "plan_version_id": "dsp_01",
        "mapping_version_id": "dmap_01",
        "projection_plan": _PLAN,
        "idempotency_key": "idem-1",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# create execution
# ---------------------------------------------------------------------------


def test_member_creates_execution_201():
    client, auth, database, role = _client(role_allowed=True)
    with (
        auth, database, role,
        patch("core.datastream_publication.create_execution", return_value=_EXEC),
        client,
    ):
        response = client.post(
            "/api/datastreams/ds_01/executions", json=_create_body()
        )
    assert response.status_code == 201
    assert response.json()["id"] == _EXEC["id"]


def test_viewer_denied_on_create_execution_404():
    client, auth, database, role = _client(role_allowed=False)
    with auth, database, role, client:
        response = client.post(
            "/api/datastreams/ds_01/executions", json=_create_body()
        )
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_create_execution_concurrent_active_409():
    from core.datastream_publication import ConcurrentExecutionActive

    client, auth, database, role = _client(role_allowed=True)
    with (
        auth, database, role,
        patch(
            "core.datastream_publication.create_execution",
            side_effect=ConcurrentExecutionActive("dse_blocking"),
        ),
        client,
    ):
        response = client.post(
            "/api/datastreams/ds_01/executions", json=_create_body()
        )
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "concurrent_execution_active"
    assert body["blocking_execution_id"] == "dse_blocking"


def test_create_execution_idempotency_conflict_409():
    from core.datastream_publication import IdempotencyConflict

    client, auth, database, role = _client(role_allowed=True)
    with (
        auth, database, role,
        patch(
            "core.datastream_publication.create_execution",
            side_effect=IdempotencyConflict(),
        ),
        client,
    ):
        response = client.post(
            "/api/datastreams/ds_01/executions", json=_create_body()
        )
    assert response.status_code == 409
    assert response.json()["code"] == "idempotency_conflict"


def test_create_execution_missing_plan_422():
    client, auth, database, role = _client(role_allowed=True)
    with auth, database, role, client:
        response = client.post(
            "/api/datastreams/ds_01/executions",
            json={"project_id": "proj_a"},
        )
    assert response.status_code == 422
    assert response.json()["code"] == "missing_field"


# ---------------------------------------------------------------------------
# publish -- route RETIRED by 26695dcc (AI-126). Strict-xfail, not deleted:
# deleted, the retirement leaves no trace and the next reader remounts the
# route. Strict-xfail turns a remount into an UNEXPECTEDLY PASSING failure, so
# the absence stays loud (motif already used for _create_datastream_mapping_
# version and the Story 50.7 share retirements).
# ---------------------------------------------------------------------------

_RETIRED_PUBLISH_REASON = (
    "26695dcc retired the direct publish route (ungoverned publication is "
    "closed; publishing goes through core.governed_publication). Kept as "
    "strict-xfail so a remount fails loudly (AI-126)."
)


@pytest.mark.xfail(strict=True, reason=_RETIRED_PUBLISH_REASON)
def test_member_publishes_successfully_200():
    client, auth, database, role = _client(role_allowed=True)
    result = {"execution": _EXEC, "publication_log_id": "dplog_1", "prior_execution_id": None}
    with (
        auth, database, role,
        patch("core.datastream_publication.run_dq_gates", return_value=[]),
        patch("core.datastream_publication.advance_state", return_value=_EXEC),
        patch("core.datastream_publication.commit_publication", return_value=result),
        client,
    ):
        # State read returns 'ready' so no validating->ready advance is attempted.
        with patch("core.db.get_connection") as gc:
            conn = MagicMock()
            cursor = MagicMock()
            cursor.fetchone.return_value = ("ready",)
            cursor.__enter__ = MagicMock(return_value=cursor)
            cursor.__exit__ = MagicMock(return_value=False)
            conn.cursor = MagicMock(return_value=cursor)
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=conn)
            ctx.__exit__ = MagicMock(return_value=False)
            gc.return_value = ctx
            response = client.post(
                "/api/datastreams/ds_01/executions/dse_01/publish",
                json={"project_id": "proj_a"},
            )
    assert response.status_code == 200
    assert response.json()["publication_log_id"] == "dplog_1"


@pytest.mark.xfail(strict=True, reason=_RETIRED_PUBLISH_REASON)
def test_member_publish_gated_delta_422():
    client, auth, database, role = _client(role_allowed=True)
    issues = [
        {
            "code": "row_count_delta_exceeded",
            "detail": "delta 100% exceeds 50%",
            "repair": {},
        }
    ]
    with (
        auth, database, role,
        patch("core.datastream_publication.run_dq_gates", return_value=issues),
        patch("core.datastream_publication.advance_state", return_value=_EXEC),
        client,
    ):
        response = client.post(
            "/api/datastreams/ds_01/executions/dse_01/publish",
            json={"project_id": "proj_a"},
        )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "dq_gate_failed"
    assert body["issues"][0]["code"] == "row_count_delta_exceeded"


@pytest.mark.xfail(strict=True, reason=_RETIRED_PUBLISH_REASON)
def test_owner_force_approves_gated_delta_succeeds_200():
    # approved=true requires Owner; role_allowed=True grants owner here.
    client, auth, database, role = _client(role_allowed=True)
    result = {
        "execution": _EXEC,
        "publication_log_id": "dplog_2",
        "prior_execution_id": "dse_prior",
    }
    with (
        auth, database, role as role_check,
        patch("core.datastream_publication.run_dq_gates", return_value=[]),
        patch("core.datastream_publication.commit_publication", return_value=result),
        client,
    ):
        with patch("core.db.get_connection") as gc:
            conn = MagicMock()
            cursor = MagicMock()
            cursor.fetchone.return_value = ("ready",)
            cursor.__enter__ = MagicMock(return_value=cursor)
            cursor.__exit__ = MagicMock(return_value=False)
            conn.cursor = MagicMock(return_value=cursor)
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=conn)
            ctx.__exit__ = MagicMock(return_value=False)
            gc.return_value = ctx
            response = client.post(
                "/api/datastreams/ds_01/executions/dse_01/publish",
                json={"project_id": "proj_a", "approved": True},
            )
    assert response.status_code == 200
    # Owner role was required for approved=true.
    assert role_check.call_args[0][2] == "owner"


def test_retired_publish_route_is_absent_from_the_asgi_app():
    """AI-126: proves the ROUTE is gone -- not that a role guard refuses.

    The test this replaces (test_publish_approved_requires_owner_role_denied_
    404) was a false green: it asserted 404 with the role check DENIED, but
    26695dcc had already removed the route, so Starlette answered 404 before
    any guard ran -- the assertion held whatever the role logic did.

    Here EVERYTHING is permitted -- auth passes, the role check grants the
    owner floor -- so the only thing left that can produce a 404 against the
    real ASGI app is the route's absence. A remount answers anything else and
    fails this test.
    """
    client, auth, database, role = _client(role_allowed=True)
    with auth, database, role, client:
        response = client.post(
            "/api/datastreams/ds_01/executions/dse_01/publish",
            json={"project_id": "proj_a", "approved": True},
        )
    assert response.status_code == 404
    # Belt and braces: no mounted route carries the retired path at all.
    from core import admin_api

    assert all(
        route.path != "/api/datastreams/{id}/executions/{exec_id}/publish"
        for route in admin_api.router.routes
    )


# ---------------------------------------------------------------------------
# state advance -- publishing/published internal-only
# ---------------------------------------------------------------------------


def test_state_advance_to_published_is_rejected_422():
    client, auth, database, role = _client(role_allowed=True)
    with auth, database, role, client:
        response = client.post(
            "/api/datastreams/ds_01/executions/dse_01/state",
            json={"project_id": "proj_a", "new_state": "published"},
        )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_state_transition"


def test_state_advance_member_loading_ok():
    client, auth, database, role = _client(role_allowed=True)
    with (
        auth, database, role,
        patch(
            "core.datastream_publication.advance_state",
            return_value={**_EXEC, "state": "loading"},
        ),
        client,
    ):
        response = client.post(
            "/api/datastreams/ds_01/executions/dse_01/state",
            json={"project_id": "proj_a", "new_state": "loading"},
        )
    assert response.status_code == 200
    assert response.json()["state"] == "loading"


# ---------------------------------------------------------------------------
# reads -- Viewer accessible
# ---------------------------------------------------------------------------


def test_viewer_reads_publication_log_200():
    client, auth, database, role = _client(role_allowed=True)
    with (
        auth, database, role,
        patch("core.datastream_publication.get_publication_log", return_value=[{"id": "dplog_1"}]),
        client,
    ):
        response = client.get(
            "/api/datastreams/ds_01/publications?project_id=proj_a"
        )
    assert response.status_code == 200
    assert response.json()["publications"][0]["id"] == "dplog_1"


def _execution_row_context(columns, row):
    """A connection whose cursor really DESCRIBES its row, like psycopg does.

    `_row_to_execution` reads `cur.description`, so a cursor that returns a dict
    would prove nothing about serialisation -- the point of the test below.
    """
    cursor = MagicMock()
    cursor.description = [(name,) for name in columns]
    cursor.fetchone.return_value = row
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cursor)
    context = MagicMock()
    context.__enter__ = MagicMock(return_value=conn)
    context.__exit__ = MagicMock(return_value=False)
    return context


def test_reading_an_execution_survives_a_timestamptz_nobody_listed_by_name():
    """Story 63.2 / 63.1: the TYPE decides, never a list of column names.

    `_row_to_execution` used to `isoformat()` exactly three hardcoded names, so
    every date-like column added afterwards (migration 218 adds `started_at`,
    `progress_updated_at` and `day_in_progress`) left a raw `datetime` in the
    payload. `admin_api` renders with the stock `JSONResponse`, whose `TypeError`
    is raised INSIDE `render()` -- outside the handler's `try/except` -- so the
    503 branch never sees it and the client gets a bare 500 that logs nothing.
    """
    from datetime import date, datetime, timezone

    columns = (
        "id", "datastream_id", "project_id", "state",
        "state_changed_at", "created_at", "updated_at",
        # The three the old list did not know about.
        "started_at", "progress_updated_at", "day_in_progress",
    )
    moment = datetime(2026, 8, 5, 2, 15, tzinfo=timezone.utc)
    row = (
        "dse_01J8ZC4Q0N7R2K3W5X6Y7Z8A9B", "ds_01", "proj_a", "loading",
        moment, moment, moment, moment, moment, date(2026, 7, 12),
    )

    app = build_asgi_app()
    # `raise_server_exceptions=False`: the render failure has to be OBSERVED as
    # a 500, not re-raised into the test.
    client = TestClient(app, raise_server_exceptions=False)
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "user-1"))),
        patch("core.db.get_connection", return_value=_execution_row_context(columns, row)),
        patch("core.project_access.identity_has_project_role", return_value=True),
        client,
    ):
        response = client.get(
            "/api/datastreams/dse_01/executions/"
            "dse_01J8ZC4Q0N7R2K3W5X6Y7Z8A9B?project_id=proj_a"
        )

    assert response.status_code == 200
    body = response.json()
    assert body["started_at"] == "2026-08-05T02:15:00+00:00"
    assert body["progress_updated_at"] == "2026-08-05T02:15:00+00:00"
    assert body["day_in_progress"] == "2026-07-12"


def test_reconcile_requires_owner_denied_404():
    client, auth, database, role = _client(role_allowed=False)
    with auth, database, role, client:
        response = client.post(
            "/api/datastreams/ds_01/executions/dse_01/reconcile",
            json={"project_id": "proj_a"},
        )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# production zero-membership default-deny
# ---------------------------------------------------------------------------


def test_production_zero_membership_denies_create():
    # identity_has_project_role returns False (no membership) -> 404 fail-closed.
    client, auth, database, role = _client(role_allowed=False)
    with auth, database, role, client:
        response = client.post(
            "/api/datastreams/ds_01/executions", json=_create_body()
        )
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
