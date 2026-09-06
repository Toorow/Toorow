from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")


class _Connection:
    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


@contextmanager
def _connection():
    yield _Connection()


def test_project_settings_get_is_registered_through_real_asgi_app() -> None:
    from core.main import build_asgi_app

    envelope = {
        "project": {"id": "proj_1", "can_edit": True},
        "capabilities": [],
        "changes": [],
    }
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_1"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=True),
        patch("core.db.get_connection", side_effect=_connection),
        patch("core.project_settings_api.read_project_settings", return_value=envelope),
    ):
        response = TestClient(build_asgi_app(), raise_server_exceptions=False).get(
            "/api/projects/proj_1/settings"
        )

    assert response.status_code == 200
    assert response.json() == envelope


def test_project_settings_cross_scope_denial_is_non_disclosing() -> None:
    from core.main import build_asgi_app

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_foreign"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=False),
        patch("core.db.get_connection", side_effect=_connection),
        patch("core.project_settings_api.read_project_settings") as read_model,
    ):
        response = TestClient(build_asgi_app(), raise_server_exceptions=False).get(
            "/api/projects/project_foreign/settings"
        )

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    read_model.assert_not_called()


# ---------------------------------------------------------------------------
# Story 48.1: the capability routes exist behind a real door, and creation no
# longer accepts caller-authored owner evidence.
# ---------------------------------------------------------------------------


class _ScopedCursor:
    """Resolves the Project's organization, which is all `_authorize` reads."""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _sql, _params=()):
        return None

    def fetchone(self):
        return ("org_1",)


class _ScopedConnection(_Connection):
    def cursor(self):
        return _ScopedCursor()


@contextmanager
def _scoped_connection():
    yield _ScopedConnection()


def _client():
    from core.main import build_asgi_app

    return TestClient(build_asgi_app(), raise_server_exceptions=False)


def test_change_set_creation_refuses_caller_authored_owner_references() -> None:
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_1"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=True),
        patch("core.db.get_connection", side_effect=_scoped_connection),
        patch("core.project_settings_api.create_change_set") as create,
    ):
        response = _client().post(
            "/api/projects/proj_1/settings/change-sets",
            headers={"Idempotency-Key": "key-1"},
            json={
                "intent": {"capabilities": {"country": "enabled"}},
                # A caller naming its own owner, version and evidence is exactly the
                # authority boundary Story 48.1 closes.
                "owner_references": [
                    {
                        "owner_kind": "governance",
                        "object_type": "registry",
                        "object_id": "fabricated",
                        "version_id": "v1",
                        "owner_route": "/anything",
                        "evidence_hash": "a" * 64,
                    }
                ],
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_project_settings"
    # The domain command is never reached, so nothing fabricated can be persisted.
    create.assert_not_called()


def test_change_set_impact_is_readable_and_never_recompiles() -> None:
    impact = {
        "schema": "project_change_set_impact.v1",
        "change_set_id": "pcset_1",
        "coverage": {},
        "matrix": [],
        "authorizing": False,
    }
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_1"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=True),
        patch("core.db.get_connection", side_effect=_scoped_connection),
        patch("core.project_settings_api.read_change_set_impact", return_value=impact) as read,
        patch("core.project_settings_api.prepare_change_set") as prepare,
    ):
        response = _client().get(
            "/api/projects/proj_1/settings/change-sets/pcset_1/impact"
        )

    assert response.status_code == 200
    assert response.json()["authorizing"] is False
    read.assert_called_once()
    # Reading an impact must never trigger a compile: what a reviewer opens is
    # what a confirmation is bound to.
    prepare.assert_not_called()


def test_both_capability_read_projections_are_mounted() -> None:
    projection = {"schema": "project_capability.v1", "datastreams": []}
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_1"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=True),
        patch("core.db.get_connection", side_effect=_scoped_connection),
        patch("core.capability_proposals.read_project_capability", return_value=projection),
        patch(
            "core.capability_proposals.read_datastream_capabilities",
            return_value={"schema": "datastream_capabilities.v1", "capabilities": []},
        ),
    ):
        client = _client()
        by_capability = client.get("/api/projects/proj_1/capabilities/country/datastreams")
        by_datastream = client.get("/api/projects/proj_1/datastreams/ds_1/capabilities")

    assert by_capability.status_code == 200
    assert by_capability.json()["schema"] == "project_capability.v1"
    assert by_datastream.status_code == 200
    assert by_datastream.json()["schema"] == "datastream_capabilities.v1"


def test_capability_projections_deny_a_foreign_scope_without_disclosure() -> None:
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_foreign"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=False),
        patch("core.db.get_connection", side_effect=_scoped_connection),
        patch("core.capability_proposals.read_project_capability") as read,
    ):
        response = _client().get("/api/projects/project_foreign/capabilities/country/datastreams")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    read.assert_not_called()
