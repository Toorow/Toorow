"""Focused Story 43.23a Connector and project-settings security contracts.

Story 47.1 renamed `/api/modules/*` to `/api/connectors/*` and removed
`_list_available_modules` / `_patch_module`. These eleven invariants -- deny
before reading, no commit or audit on denial, a non-disclosing 404 for an unknown
name, a sanitized 503 -- were left importing the removed symbols and so proved
nothing. They are re-expressed here against the handlers production calls, which
is what the Epic 46 review asked for when the same thing happened to the
strict-access helpers. The object is a CONNECTOR
(docs/product-architecture/glossary.md); `app.project_modules` survives only as
a not-yet-migrated column name.
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.datastructures import QueryParams


def _request(*, query=None, path=None, body=None):
    request = MagicMock()
    request.query_params = QueryParams(query or {})
    request.path_params = path or {}
    payload = body if isinstance(body, bytes) else json.dumps(body or {}).encode()
    request.body = AsyncMock(return_value=payload)
    return request


def _cursor_context(*, fetchone=None, fetchall=None):
    cursor = MagicMock()
    cursor.fetchone.return_value = fetchone
    cursor.fetchall.return_value = fetchall or []
    context = MagicMock()
    context.__enter__.return_value = cursor
    context.__exit__.return_value = False
    return context, cursor


@pytest.mark.anyio
async def test_connector_catalog_cross_project_denial_precedes_domain_read():
    from core.catalog_api import _list_available_connectors  # noqa: PLC0415

    conn = MagicMock()
    discovery = MagicMock()
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_a"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=False) as guard,
        patch("core.catalog_api._connector_discovery_catalog", discovery),
        patch("core.db.get_connection", return_value=nullcontext(conn)),
        patch("core.projects_api.write_audit_row") as audit,
    ):
        response = await _list_available_connectors(
            _request(query={"project_id": "foreign_project"})
        )

    assert response.status_code == 404
    assert json.loads(response.body) == {
        "code": "not_found",
        "message": "Project not found",
    }
    assert guard.call_args.kwargs["minimum_capability"] == "view"
    discovery.assert_not_called()
    conn.cursor.assert_not_called()
    audit.assert_not_called()


@pytest.mark.anyio
async def test_connector_catalog_database_failure_is_honest_503():
    from core.catalog_api import _list_available_connectors  # noqa: PLC0415

    conn = MagicMock()
    conn.cursor.side_effect = RuntimeError("database hostname and password")
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_a"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=True),
        patch(
            "core.catalog_api._connector_discovery_catalog",
            return_value=[{"name": "known", "display_name": "Known"}],
        ),
        patch("core.db.get_connection", return_value=nullcontext(conn)),
    ):
        response = await _list_available_connectors(
            _request(query={"project_id": "project_a"})
        )

    assert response.status_code == 503
    payload = json.loads(response.body)
    assert payload == {
        "code": "db_error",
        "message": "Connector settings are temporarily unavailable",
    }
    assert "password" not in response.body.decode().lower()


@pytest.mark.anyio
@pytest.mark.parametrize("denial", ["cross_project", "viewer_without_edit"])
async def test_connector_patch_denial_has_no_mutation_commit_or_audit(denial):
    from core.catalog_api import _patch_connector  # noqa: PLC0415

    conn = MagicMock()
    discovery = MagicMock()
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, denial))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=False) as guard,
        patch("core.catalog_api._connector_discovery_catalog", discovery),
        patch("core.db.get_connection", return_value=nullcontext(conn)),
        patch("core.projects_api.write_audit_row") as audit,
    ):
        response = await _patch_connector(
            _request(
                path={"project_id": "project_a", "connector_name": "known"},
                body={"enabled": True},
            )
        )

    assert response.status_code == 404
    assert guard.call_args.kwargs["minimum_capability"] == "edit"
    discovery.assert_not_called()
    conn.cursor.assert_not_called()
    conn.commit.assert_not_called()
    audit.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [pytest.param({"enabled": "false"}, id="string"), pytest.param({"enabled": 1}, id="int")],
)
async def test_connector_patch_requires_exact_json_boolean(body):
    from core.catalog_api import _patch_connector  # noqa: PLC0415

    get_connection = MagicMock()
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "editor"))),
        patch("core.db.get_connection", get_connection),
    ):
        response = await _patch_connector(
            _request(
                path={"project_id": "project_a", "connector_name": "known"},
                body=body,
            )
        )

    assert response.status_code == 400
    assert json.loads(response.body)["code"] == "invalid_field"
    get_connection.assert_not_called()


@pytest.mark.anyio
async def test_connector_patch_unknown_discovered_name_is_nondisclosing_404():
    from core.catalog_api import _patch_connector  # noqa: PLC0415

    conn = MagicMock()
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "editor"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=True),
        patch(
            "core.catalog_api._connector_discovery_catalog",
            return_value=[{"name": "known", "display_name": "Known"}],
        ),
        patch("core.db.get_connection", return_value=nullcontext(conn)),
        patch("core.projects_api.write_audit_row") as audit,
    ):
        response = await _patch_connector(
            _request(
                path={"project_id": "project_a", "connector_name": "typo"},
                body={"enabled": True},
            )
        )

    assert response.status_code == 404
    conn.cursor.assert_not_called()
    conn.commit.assert_not_called()
    audit.assert_not_called()


@pytest.mark.anyio
async def test_connector_patch_editor_updates_known_module_once():
    from core.catalog_api import _patch_connector  # noqa: PLC0415

    count_context, count_cursor = _cursor_context(fetchone=(2,))
    upsert_context, upsert_cursor = _cursor_context(fetchone=("known", False))
    conn = MagicMock()
    conn.cursor.side_effect = [count_context, upsert_context]
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "editor"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=True) as guard,
        patch(
            "core.catalog_api._connector_discovery_catalog",
            return_value=[{"name": "known", "display_name": "Known"}],
        ),
        patch("core.db.get_connection", return_value=nullcontext(conn)),
    ):
        response = await _patch_connector(
            _request(
                path={"project_id": "project_a", "connector_name": "known"},
                body={"enabled": False},
            )
        )

    assert response.status_code == 200
    assert json.loads(response.body)["enabled"] is False
    assert guard.call_args.kwargs["minimum_capability"] == "edit"
    assert "SELECT COUNT" in count_cursor.execute.call_args.args[0]
    assert "INSERT INTO app.project_modules" in upsert_cursor.execute.call_args.args[0]
    conn.commit.assert_called_once_with()


@pytest.mark.anyio
async def test_project_patch_viewer_denial_has_no_read_mutation_or_refusal_audit():
    from core.projects_api import _patch_project  # noqa: PLC0415

    conn = MagicMock()
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "viewer"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=False) as guard,
        patch("core.db.get_connection", return_value=nullcontext(conn)),
        patch("core.projects_api._fetch_geographic_prefs") as geography,
        patch("core.projects_api.write_audit_row") as audit,
    ):
        response = await _patch_project(
            _request(path={"project_id": "project_a"}, body={"name": "Changed"})
        )

    assert response.status_code == 404
    assert guard.call_args.kwargs["minimum_capability"] == "edit"
    geography.assert_not_called()
    conn.cursor.assert_not_called()
    conn.commit.assert_not_called()
    audit.assert_not_called()


def test_project_settings_edit_preserves_auth_disabled_anonymous_compatibility(monkeypatch):
    """`make dev` still edits its local project without a grant -- on a project
    that EXISTS.

    This pinned `conn.cursor.assert_not_called()`: the bypass touched nothing.
    It now runs one existence probe, because granting every capability on every
    string made an unknown project id read as an empty one (live finding C1).
    The compatibility this test defends is the local workflow, not the absence
    of a lookup.
    """
    from core.admin_api import _strict_project_capability_allowed

    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    conn = MagicMock()

    assert _strict_project_capability_allowed(
        conn,
        identity="anonymous",
        project_id="local_project",
        minimum_capability="edit",
    )
    sql = conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0]
    assert "FROM app.projects" in sql

@pytest.mark.anyio
async def test_connector_patch_database_failure_is_sanitized_503_without_commit():
    from core.catalog_api import _patch_connector  # noqa: PLC0415

    conn = MagicMock()
    conn.cursor.side_effect = RuntimeError("database hostname and password")
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "editor"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=True),
        patch(
            "core.catalog_api._connector_discovery_catalog",
            return_value=[{"name": "known", "display_name": "Known"}],
        ),
        patch("core.db.get_connection", return_value=nullcontext(conn)),
    ):
        response = await _patch_connector(
            _request(
                path={"project_id": "project_a", "connector_name": "known"},
                body={"enabled": True},
            )
        )

    assert response.status_code == 503
    assert json.loads(response.body) == {
        "code": "db_error",
        "message": "Connector settings are temporarily unavailable",
    }
    assert "password" not in response.body.decode().lower()
    conn.commit.assert_not_called()

@pytest.mark.anyio
@pytest.mark.parametrize(
    ("identity", "can_edit"),
    [("owner", True), ("viewer", False)],
)
async def test_project_detail_exposes_strict_can_edit_capability(identity, can_edit):
    from core.projects_api import _get_project  # noqa: PLC0415

    cursor_context, cursor = _cursor_context(fetchone=("project_a",))
    conn = MagicMock()
    conn.cursor.return_value = cursor_context
    geography = MagicMock()
    geography.as_dict.return_value = {}
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, identity))),
        patch(
            "core.admin_api._strict_project_capability_allowed",
            side_effect=[True, can_edit],
        ) as guard,
        patch("core.db.get_connection", return_value=nullcontext(conn)),
        patch("core.projects_api._project_row_to_dict", return_value={"id": "project_a"}),
        patch("core.projects_api._fetch_verification_prefs", return_value={}),
        patch("core.projects_api._fetch_geographic_prefs", return_value=geography),
    ):
        response = await _get_project(_request(path={"project_id": "project_a"}))

    assert response.status_code == 200
    assert json.loads(response.body)["can_edit"] is can_edit
    assert [call.kwargs["minimum_capability"] for call in guard.call_args_list] == [
        "view",
        "edit",
    ]
    assert "FROM app.projects" in cursor.execute.call_args.args[0]
