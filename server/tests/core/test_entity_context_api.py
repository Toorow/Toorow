"""The two doors of the discovery read -- Story 68.7, AI-56.

Proven at the `build_asgi_app()` seam WITHOUT a database, the discipline of
`test_entity_types_api.py`: what is proven here is the posture (401 / 404 /
200 / 503) and the single-writer contract -- the REST route and the MCP tool
return the SAME payload because both serve
`object_kind_registry.describe_entity_reconciliation_context` and shape
nothing themselves.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

IDENTITY = "owner@example.com"
ORG = "org_EXAMPLE"
PROJECT = "proj_EXAMPLE"
BASE = f"/api/projects/{PROJECT}/master-data/entity-reconciliation-context"

#: What the ONE read returns; both doors must serve it verbatim.
SENTINEL = {
    "schema_version": "1",
    "meta": {
        "freshness": "live",
        "provenance": {
            "source_system": "connector-core",
            "source_field": "entity_reconciliation_context",
            "pull_id": None,
        },
        "alerts": [
            {
                "severity": "info",
                "code": "entity_context_partial",
                "message": "Sections unavailable (named, never zeroed): matching_coverage",
            }
        ],
    },
    "data": {
        "project_id": PROJECT,
        "entity_types": {"status": "ok", "count": 0, "items": [], "empty_reason": {}},
        "bindings": {"status": "ok", "designation_count": 0},
        "rule_sets": {"status": "unavailable", "reason": {"code": "x"}},
        "matching_coverage": {"status": "unavailable", "reason": {"code": "y"}},
        "unattached": [],
    },
}


class _Cursor:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        return None

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _Connection:
    def cursor(self):
        return _Cursor()

    def commit(self):
        return None

    def rollback(self):
        return None


@contextmanager
def _fake_connection(_identity=None):
    yield _Connection()


def _serving(allowed=True):
    from core.project_access import AccessDecision

    decision = (
        AccessDecision(True, "ok", capability="manage", org_id=ORG)
        if allowed
        else AccessDecision(False, "not_found")
    )
    return patch("core.project_access.resolve_strict_resource_access", return_value=decision)


def _client():
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    return TestClient(build_asgi_app(), raise_server_exceptions=False)


def _auth_ok():
    return patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, IDENTITY)))


def _read(return_value=SENTINEL):
    return patch(
        "core.object_kind_registry.describe_entity_reconciliation_context",
        return_value=return_value,
    )


# ===========================================================================
# The address exists
# ===========================================================================


def test_the_route_is_mounted_under_the_project_address():
    from core.admin_api import router

    paths = {
        getattr(route, "path", ""): getattr(route, "methods", set()) for route in router.routes
    }
    assert "GET" in paths.get(
        "/api/projects/{project_id}/master-data/entity-reconciliation-context", set()
    )


# ===========================================================================
# The posture: 401, 404, 200, 503 -- and never one for another.
# ===========================================================================


def test_no_authentication_is_no_answer():
    with patch("core.admin_api._check_auth", new=AsyncMock(return_value=(False, ""))):
        with _client() as client:
            assert client.get(BASE).status_code == 401


def test_a_project_the_org_does_not_own_is_404_and_never_403():
    with (
        _auth_ok(),
        patch("core.query_specs_api.analyze_connection", _fake_connection),
        _serving(allowed=False),
        _client() as client,
    ):
        response = client.get(BASE)
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_a_read_serves_the_envelope_verbatim():
    with (
        _auth_ok(),
        patch("core.query_specs_api.analyze_connection", _fake_connection),
        _serving(),
        _read(),
        _client() as client,
    ):
        response = client.get(BASE)
    assert response.status_code == 200
    assert response.json() == SENTINEL


def test_a_read_that_fails_is_503_and_carries_no_data():
    def _boom(*_a, **_k):
        raise RuntimeError("the store did not answer")

    with (
        _auth_ok(),
        patch("core.query_specs_api.analyze_connection", _fake_connection),
        _serving(),
        patch(
            "core.object_kind_registry.describe_entity_reconciliation_context",
            side_effect=_boom,
        ),
        _client() as client,
    ):
        response = client.get(BASE)
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "entity_context_unavailable"
    assert "data" not in body
    assert "entity_types" not in body


# ===========================================================================
# Single writer, two doors: the MCP tool serves the SAME payload (AC3).
# ===========================================================================

_TOKEN = SimpleNamespace(claims={"sub": IDENTITY}, client_id="client_EXAMPLE")


def _mcp_patches(*, allowed=True):
    return (
        patch("fastmcp.server.dependencies.get_access_token", return_value=_TOKEN),
        patch("core.db.request_connection", _fake_connection),
        _serving(allowed=allowed),
        _read(),
    )


def test_the_mcp_tool_returns_the_same_payload_as_the_rest_door():
    from core.entity_context_mcp import list_entity_reconciliation_context

    conn, access, read = _mcp_patches()[1:]
    with (
        patch("fastmcp.server.dependencies.get_access_token", return_value=_TOKEN),
        conn,
        access,
        read,
    ):
        payload = list_entity_reconciliation_context(PROJECT)
    assert payload == SENTINEL


def test_the_mcp_tool_denies_an_anonymous_caller_as_not_found():
    from core.entity_context_mcp import list_entity_reconciliation_context
    from fastmcp.exceptions import ToolError

    with patch("fastmcp.server.dependencies.get_access_token", return_value=None):
        with pytest.raises(ToolError) as excinfo:
            list_entity_reconciliation_context(PROJECT)
    assert json.loads(str(excinfo.value))["code"] == "project_not_found"


def test_the_mcp_tool_denies_a_foreign_project_as_not_found():
    from core.entity_context_mcp import list_entity_reconciliation_context
    from fastmcp.exceptions import ToolError

    conn, access, read = _mcp_patches(allowed=False)[1:]
    with (
        patch("fastmcp.server.dependencies.get_access_token", return_value=_TOKEN),
        conn,
        access,
        read,
    ):
        with pytest.raises(ToolError) as excinfo:
            list_entity_reconciliation_context(PROJECT)
    assert json.loads(str(excinfo.value))["code"] == "project_not_found"


def test_the_mcp_tool_names_unavailable_when_the_read_fails():
    from core.entity_context_mcp import list_entity_reconciliation_context
    from fastmcp.exceptions import ToolError

    def _boom(*_a, **_k):
        raise RuntimeError("the store did not answer")

    with (
        patch("fastmcp.server.dependencies.get_access_token", return_value=_TOKEN),
        patch("core.db.request_connection", _fake_connection),
        _serving(),
        patch(
            "core.object_kind_registry.describe_entity_reconciliation_context",
            side_effect=_boom,
        ),
    ):
        with pytest.raises(ToolError) as excinfo:
            list_entity_reconciliation_context(PROJECT)
    assert json.loads(str(excinfo.value))["code"] == "entity_context_unavailable"


def test_the_tool_is_registered_with_the_insights_read_declaration():
    """The catalogue declaration: insights profile, read effect, no confirmation."""
    import core.main  # noqa: PLC0415,F401 -- registering is an import side effect
    from core.mcp_profiles import registered_declarations

    declared = {tool.name: tool for tool in registered_declarations()}
    tool = declared["list_entity_reconciliation_context"]
    assert tool.profile == "insights"
    assert tool.effect == "read"
    assert tool.confirmation_mode == "none"
