"""Real-ASGI seams for the atomic publication routes (Story 12.5, AI-56).

Through build_asgi_app(): Member creates an execution (201); Viewer is read-only
(404 on mutation routes); cross-project / zero-membership returns a non-disclosing
404; Member publishes successfully; a gated row-count delta returns 422; Owner
force-approve succeeds; a concurrent execution returns 409. The DB is mocked at the
core.db.get_connection boundary and the publication-module functions are patched so
these run WITHOUT Postgres (the live transaction is proven in the pg-gated
constraints test). Kept deliberately small (AI-60).
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.testclient import TestClient

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core.main import build_asgi_app  # noqa: E402


def _db_context():
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = None
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
# publish
# ---------------------------------------------------------------------------


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


def test_publish_approved_requires_owner_role_denied_404():
    # role denied (not owner) on the approved path -> non-disclosing 404.
    client, auth, database, role = _client(role_allowed=False)
    with auth, database, role, client:
        response = client.post(
            "/api/datastreams/ds_01/executions/dse_01/publish",
            json={"project_id": "proj_a", "approved": True},
        )
    assert response.status_code == 404


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
