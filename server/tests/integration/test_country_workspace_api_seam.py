from __future__ import annotations

import inspect
import os
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from core.governance_surface_api import (
    GOVERNANCE_SURFACE_ROUTES,
    _country_workspace_command,
    _country_workspace_read,
)
from core.project_access import AccessDecision
from starlette.testclient import TestClient

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")

PATH = "/api/projects/{project_id}/governance/master-data/country"
URL = "/api/projects/proj_EXAMPLE/governance/master-data/country"


class _Connection:
    def __init__(self):
        self.commit = MagicMock()


@contextmanager
def _connection():
    yield _Connection()


def _client():
    from core.main import build_asgi_app

    return TestClient(build_asgi_app(), raise_server_exceptions=False)


def _allowed(capability: str):
    return AccessDecision(True, "explicit_grant", capability, "org_EXAMPLE")


def test_country_workspace_read_and_command_are_mounted_without_a_shadow_route():
    matching = [route for route in GOVERNANCE_SURFACE_ROUTES if route.path == PATH]

    assert len(matching) == 2
    assert any("GET" in route.methods for route in matching)
    assert any("POST" in route.methods for route in matching)
    generic = "/api/projects/{project_id}/governance/{section}"
    assert [route.path for route in GOVERNANCE_SURFACE_ROUTES].index(PATH) < [
        route.path for route in GOVERNANCE_SURFACE_ROUTES
    ].index(generic)


def test_country_read_requires_view_and_command_requires_edit():
    read_source = inspect.getsource(_country_workspace_read)
    command_source = inspect.getsource(_country_workspace_command)

    assert 'minimum_capability="view"' in read_source
    capability_expression = (
        '"manage" if requested_action in {"prepare_publish", "publish"} else "edit"'
    )
    assert capability_expression in command_source
    assert "Idempotency-Key" in command_source


def test_country_workspace_get_reaches_the_real_read_model():
    envelope = {
        "state": "preset_required",
        "registry": None,
        "presets": [{"id": "france-and-territories"}],
        "vocabulary": [],
    }
    with (
        patch(
            "core.admin_api._check_auth",
            new=AsyncMock(return_value=(True, "person@example.com")),
        ),
        patch("core.db.install_access_context"),
        patch(
            "core.governance_surface_api.resolve_strict_resource_access",
            return_value=_allowed("view"),
        ) as access,
        patch("core.db.get_connection", side_effect=_connection),
        patch(
            "core.country_workspace.load_country_workspace",
            return_value=envelope,
        ) as load,
    ):
        response = _client().get(URL)

    assert response.status_code == 200
    assert response.json() == envelope
    assert response.headers["cache-control"] == "no-store"
    load.assert_called_once()
    assert load.call_args.kwargs["project_id"] == "proj_EXAMPLE"
    assert access.call_args.kwargs["minimum_capability"] == "view"


def test_country_workspace_post_reaches_the_audited_command():
    connection = _Connection()

    @contextmanager
    def command_connection():
        yield connection
    result = {
        "operation_id": "op_1",
        "outcome": "succeeded",
        "result": {"draft_version_id": "mdv_1"},
        "idempotent_replay": False,
    }
    with (
        patch(
            "core.admin_api._check_auth",
            new=AsyncMock(return_value=(True, "person@example.com")),
        ),
        patch("core.db.install_access_context"),
        patch(
            "core.governance_surface_api.resolve_strict_resource_access",
            return_value=_allowed("edit"),
        ) as access,
        patch("core.db.get_connection", side_effect=command_connection),
        patch(
            "core.country_workspace_commands.run_country_workspace_command",
            return_value=result,
        ) as run,
    ):
        response = _client().post(
            URL,
            headers={"Idempotency-Key": "country-op-1"},
            json={
                "action": "apply_preset",
                "payload": {"preset_id": "france-and-territories"},
            },
        )

    assert response.status_code == 200
    assert response.json() == result
    assert run.call_args.kwargs["project_id"] == "proj_EXAMPLE"
    assert run.call_args.kwargs["org_id"] == "org_EXAMPLE"
    assert run.call_args.kwargs["idempotency_key"] == "country-op-1"
    assert access.call_args.kwargs["minimum_capability"] == "edit"
    connection.commit.assert_called_once_with()


def test_country_workspace_post_requires_an_idempotency_key():
    with patch(
        "core.admin_api._check_auth",
        new=AsyncMock(return_value=(True, "person@example.com")),
    ):
        response = _client().post(
            URL,
            json={"action": "apply_preset", "payload": {}},
        )

    assert response.status_code == 428
    assert response.json()["code"] == "idempotency_key_required"

def test_prepare_publish_requires_manage_and_commits_the_confirmation():
    connection = _Connection()

    @contextmanager
    def selected_connection():
        yield connection

    prepared = {
        "confirmation_id": "econf_1",
        "confirmation_secret": "single-use-secret",
        "version_id": "mdv_1",
        "expected_content_hash": "a" * 64,
    }
    with (
        patch(
            "core.admin_api._check_auth",
            new=AsyncMock(return_value=(True, "person_1")),
        ),
        patch("core.db.install_access_context"),
        patch(
            "core.governance_surface_api.resolve_strict_resource_access",
            return_value=_allowed("manage"),
        ) as access,
        patch("core.db.get_connection", side_effect=selected_connection),
        patch(
            "core.country_workspace_commands.prepare_country_publish_confirmation",
            return_value=prepared,
        ) as prepare,
    ):
        response = _client().post(
            URL,
            headers={"Idempotency-Key": "country-publish-1"},
            json={
                "action": "prepare_publish",
                "payload": {
                    "version_id": "mdv_1",
                    "expected_content_hash": "a" * 64,
                },
            },
        )

    assert response.status_code == 200
    assert response.json() == prepared
    assert access.call_args.kwargs["minimum_capability"] == "manage"
    prepare.assert_called_once()
    connection.commit.assert_called_once_with()
