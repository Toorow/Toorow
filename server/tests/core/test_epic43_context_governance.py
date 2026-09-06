"""Focused contracts for Epic 43.22b context governance."""

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
    request.json = AsyncMock(return_value={} if body is None else body)
    return request


def _topic(*, project_id="project_a", version=2):
    return {
        "id": "top_1",
        "project_id": project_id,
        "title": "Topic",
        "body_md": "Body",
        "status": "active",
        "owner": None,
        "created_by": "p1",
        "created_at": "2026-07-26T00:00:00Z",
        "updated_at": "2026-07-26T00:00:00Z",
        "version_number": version,
    }


def _procedure(*, project_id="project_a", version=2):
    return {
        "id": "proc_1",
        "project_id": project_id,
        "name": "procedure",
        "description": "Description",
        "frontmatter_yaml": "name: procedure\ndescription: Description",
        "body_md": "Body",
        "status": "active",
        "owner": None,
        "created_by": "p1",
        "created_at": "2026-07-26T00:00:00Z",
        "updated_at": "2026-07-26T00:00:00Z",
        "version_number": version,
    }


@pytest.mark.anyio
async def test_member_of_both_projects_cannot_patch_object_outside_url_project():
    """Membership in A and B does not let a project-A URL mutate a project-B row."""
    from core.context_api import _update_topic

    update = MagicMock()
    get_topic = MagicMock(return_value=None)
    with (
        patch("core.context_api._check_auth", new=AsyncMock(return_value=(True, "p1"))),
        patch("core.context_api._strict_url_project_allowed", return_value=True),
        patch("core.db.get_connection", return_value=nullcontext(MagicMock())),
        patch("core.context_store.get_topic", get_topic),
        patch("core.context_store.update_topic", update),
    ):
        response = await _update_topic(
            _request(
                query={"project_id": "project_a"},
                path={"id": "topic_owned_by_b"},
                body={"expected_version": 4, "title": "Cross scope"},
            )
        )

    assert response.status_code == 404
    assert get_topic.call_args.kwargs["caller_project_id"] == "project_a"
    update.assert_not_called()


@pytest.mark.anyio
async def test_topic_patch_stale_version_returns_409():
    from core.context_api import _update_topic
    from core.context_store import StaleContextVersionError

    with (
        patch("core.context_api._check_auth", new=AsyncMock(return_value=(True, "p1"))),
        patch("core.context_api._strict_url_project_allowed", return_value=True),
        patch("core.db.get_connection", return_value=nullcontext(MagicMock())),
        patch("core.context_store.get_topic", return_value=_topic(version=3)),
        patch(
            "core.context_store.update_topic",
            side_effect=StaleContextVersionError("Expected version 2, current version 3."),
        ),
    ):
        response = await _update_topic(
            _request(
                query={"project_id": "project_a"},
                path={"id": "top_1"},
                body={"expected_version": 2, "title": "Stale"},
            )
        )

    assert response.status_code == 409
    assert json.loads(response.body)["code"] == "version_conflict"


@pytest.mark.anyio
async def test_procedure_archive_stale_version_returns_409():
    from core.context_api import _archive_procedure
    from core.context_store import StaleContextVersionError

    with (
        patch("core.context_api._check_auth", new=AsyncMock(return_value=(True, "p1"))),
        patch("core.context_api._strict_url_project_allowed", return_value=True),
        patch("core.db.get_connection", return_value=nullcontext(MagicMock())),
        patch("core.context_store.get_procedure", return_value=_procedure(version=3)),
        patch(
            "core.context_store.archive_procedure",
            side_effect=StaleContextVersionError("Expected version 2, current version 3."),
        ),
    ):
        response = await _archive_procedure(
            _request(
                query={"project_id": "project_a"},
                path={"id": "proc_1"},
                body={"expected_version": 2},
            )
        )

    assert response.status_code == 409
    assert json.loads(response.body)["code"] == "version_conflict"


def test_store_checks_expected_version_after_for_update_before_mutation():
    from core.context_store import StaleContextVersionError, update_topic

    cursor = MagicMock()
    cursor.description = [
        ("id",), ("project_id",), ("title",), ("body_md",), ("status",),
        ("owner",), ("created_by",), ("created_at",), ("updated_at",),
    ]
    cursor.fetchone.side_effect = [
        ("top_1", "project_a", "Topic", "Body", "active", None, "p1",
         "2026-07-26T00:00:00Z", "2026-07-26T00:00:00Z"),
        (3,),
    ]
    cursor_cm = MagicMock()
    cursor_cm.__enter__.return_value = cursor
    cursor_cm.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cursor_cm

    with pytest.raises(StaleContextVersionError):
        update_topic(
            conn,
            topic_id="top_1",
            patch={"title": "Stale"},
            changed_by="p1",
            expected_version=2,
        )

    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert "FOR UPDATE" in statements[0]
    assert "MAX(version_number)" in statements[1]
    assert not any("UPDATE app.context_topics" in sql for sql in statements)


@pytest.mark.anyio
async def test_topic_versions_require_view_and_return_immutable_history():
    from core.context_api import _list_topic_versions

    guard = MagicMock(return_value=True)
    versions = [{"topic_id": "top_1", "version_number": 2, "changed_by": "p1"}]
    with (
        patch("core.context_api._check_auth", new=AsyncMock(return_value=(True, "p1"))),
        patch("core.context_api._strict_url_project_allowed", guard),
        patch("core.db.get_connection", return_value=nullcontext(MagicMock())),
        patch("core.context_store.get_topic", return_value=_topic()),
        patch("core.context_store.list_topic_versions", return_value=versions),
    ):
        response = await _list_topic_versions(
            _request(query={"project_id": "project_a"}, path={"id": "top_1"})
        )

    assert response.status_code == 200
    assert json.loads(response.body)["versions"] == versions
    assert guard.call_args.kwargs["minimum_capability"] == "view"


@pytest.mark.anyio
async def test_procedure_versions_hide_foreign_object():
    from core.context_api import _list_procedure_versions

    history = MagicMock()
    with (
        patch("core.context_api._check_auth", new=AsyncMock(return_value=(True, "p1"))),
        patch("core.context_api._strict_url_project_allowed", return_value=True),
        patch("core.db.get_connection", return_value=nullcontext(MagicMock())),
        patch("core.context_store.get_procedure", return_value=None),
        patch("core.context_store.list_procedure_versions", history),
    ):
        response = await _list_procedure_versions(
            _request(query={"project_id": "project_a"}, path={"id": "proc_b"})
        )

    assert response.status_code == 404
    history.assert_not_called()


@pytest.mark.anyio
async def test_topic_list_capabilities_distinguish_project_and_platform_rows():
    from core.context_api import _list_topics

    guard = MagicMock(side_effect=[True, True])
    rows = [_topic(project_id="project_a"), _topic(project_id=None)]
    with (
        patch("core.context_api._check_auth", new=AsyncMock(return_value=(True, "p1"))),
        patch("core.context_api._strict_url_project_allowed", guard),
        patch("core.context_api.check_platform_write_authorized", return_value=False),
        patch("core.db.get_connection", return_value=nullcontext(MagicMock())),
        patch("core.context_store.list_topics", return_value=rows),
    ):
        response = await _list_topics(_request(query={"project_id": "project_a"}))

    payload = json.loads(response.body)
    assert payload["capabilities"] == {
        "can_write": True,
        "version_history": True,
        "usage": False,
    }
    assert payload["topics"][0]["capabilities"]["can_write"] is True
    assert payload["topics"][1]["capabilities"]["can_write"] is False
    assert all(row["capabilities"]["usage"] is False for row in payload["topics"])


@pytest.mark.anyio
@pytest.mark.parametrize("handler_name", ["_list_topics", "_list_procedures"])
async def test_context_lists_require_project_before_database(handler_name):
    from core import context_api

    db = MagicMock()
    with patch("core.context_api._check_auth", new=AsyncMock(return_value=(True, "p1"))), patch(
        "core.db.get_connection", db
    ):
        response = await getattr(context_api, handler_name)(_request())

    assert response.status_code == 422
    db.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize("handler_name", ["_create_topic", "_create_procedure"])
async def test_context_creates_require_project_before_database(handler_name):
    from core import context_api

    body = {"title": "Topic"} if handler_name == "_create_topic" else {
        "frontmatter_yaml": "name: procedure\ndescription: Description"
    }
    db = MagicMock()
    with patch("core.context_api._check_auth", new=AsyncMock(return_value=(True, "p1"))), patch(
        "core.db.get_connection", db
    ):
        response = await getattr(context_api, handler_name)(_request(body=body))

    assert response.status_code == 422
    db.assert_not_called()

@pytest.mark.anyio
@pytest.mark.parametrize(
    ("handler_name", "store_name", "body"),
    [
        ("_create_topic", "create_topic", {"title": "Topic"}),
        (
            "_create_procedure",
            "create_procedure",
            {"frontmatter_yaml": "name: procedure\ndescription: Description"},
        ),
    ],
)
async def test_context_create_uses_authorized_url_project_when_body_omits_project(
    handler_name, store_name, body
):
    from core import context_api

    created = _topic(version=1) if handler_name == "_create_topic" else _procedure(version=1)
    store = MagicMock(return_value=created)
    guard = MagicMock(return_value=True)
    with (
        patch("core.context_api._check_auth", new=AsyncMock(return_value=(True, "p1"))),
        patch("core.context_api._strict_url_project_allowed", guard),
        patch("core.db.get_connection", return_value=nullcontext(MagicMock())),
        patch(f"core.context_store.{store_name}", store),
    ):
        response = await getattr(context_api, handler_name)(
            _request(query={"project_id": "project_a"}, body=body)
        )

    assert response.status_code == 201
    assert guard.call_args.kwargs == {
        "identity": "p1",
        "project_id": "project_a",
        "minimum_capability": "edit",
    }
    assert store.call_args.kwargs["project_id"] == "project_a"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("handler_name", "store_name", "body"),
    [
        ("_create_topic", "create_topic", {"project_id": "project_b", "title": "Topic"}),
        (
            "_create_procedure",
            "create_procedure",
            {
                "project_id": "project_b",
                "frontmatter_yaml": "name: procedure\ndescription: Description",
            },
        ),
    ],
)
async def test_context_create_rejects_body_project_mismatch_after_url_authorization(
    handler_name, store_name, body
):
    from core import context_api

    store = MagicMock()
    guard = MagicMock(return_value=True)
    with (
        patch("core.context_api._check_auth", new=AsyncMock(return_value=(True, "p1"))),
        patch("core.context_api._strict_url_project_allowed", guard),
        patch("core.db.get_connection", return_value=nullcontext(MagicMock())),
        patch(f"core.context_store.{store_name}", store),
    ):
        response = await getattr(context_api, handler_name)(
            _request(query={"project_id": "project_a"}, body=body)
        )

    assert response.status_code == 404
    assert json.loads(response.body)["code"] == "not_found"
    guard.assert_called_once()
    store.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize("handler_name", ["_create_topic", "_create_procedure"])
async def test_context_create_rejects_non_string_body_project_before_database(handler_name):
    from core import context_api

    body = {"project_id": 42, "title": "Topic"}
    db = MagicMock()
    with patch("core.context_api._check_auth", new=AsyncMock(return_value=(True, "p1"))), patch(
        "core.db.get_connection", db
    ):
        response = await getattr(context_api, handler_name)(
            _request(query={"project_id": "project_a"}, body=body)
        )

    assert response.status_code == 422
    assert json.loads(response.body)["code"] == "invalid_param"
    db.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("handler_name", "path"),
    [
        ("_update_topic", {"id": "top_1"}),
        ("_archive_topic", {"id": "top_1"}),
        ("_update_procedure", {"id": "proc_1"}),
        ("_archive_procedure", {"id": "proc_1"}),
    ],
)
@pytest.mark.parametrize("payload", ["primitive", 7, []])
async def test_context_mutations_reject_json_primitives_before_database(
    handler_name, path, payload
):
    from core import context_api

    db = MagicMock()
    with patch("core.context_api._check_auth", new=AsyncMock(return_value=(True, "p1"))), patch(
        "core.db.get_connection", db
    ):
        response = await getattr(context_api, handler_name)(
            _request(query={"project_id": "project_a"}, path=path, body=payload)
        )

    assert response.status_code == 422
    assert json.loads(response.body) == {
        "code": "invalid_body",
        "message": "JSON object body required",
    }
    db.assert_not_called()

@pytest.mark.anyio
@pytest.mark.parametrize(
    ("handler_name", "path", "body", "get_name", "mutation_name"),
    [
        (
            "_update_topic",
            {"id": "top_1"},
            {"project_id": "project_b", "expected_version": 2, "title": "No"},
            "get_topic",
            "update_topic",
        ),
        (
            "_archive_topic",
            {"id": "top_1"},
            {"project_id": "project_b", "expected_version": 2},
            "get_topic",
            "archive_topic",
        ),
        (
            "_update_procedure",
            {"id": "proc_1"},
            {"project_id": "project_b", "expected_version": 2, "body_md": "No"},
            "get_procedure",
            "update_procedure",
        ),
        (
            "_archive_procedure",
            {"id": "proc_1"},
            {"project_id": "project_b", "expected_version": 2},
            "get_procedure",
            "archive_procedure",
        ),
    ],
)
async def test_context_mutations_reject_body_project_mismatch_after_url_authorization(
    handler_name, path, body, get_name, mutation_name
):
    from core import context_api

    lookup = MagicMock()
    mutation = MagicMock()
    with (
        patch("core.context_api._check_auth", new=AsyncMock(return_value=(True, "p1"))),
        patch("core.context_api._strict_url_project_allowed", return_value=True) as guard,
        patch("core.db.get_connection", return_value=nullcontext(MagicMock())),
        patch(f"core.context_store.{get_name}", lookup),
        patch(f"core.context_store.{mutation_name}", mutation),
    ):
        response = await getattr(context_api, handler_name)(
            _request(query={"project_id": "project_a"}, path=path, body=body)
        )

    assert response.status_code == 404
    guard.assert_called_once()
    lookup.assert_not_called()
    mutation.assert_not_called()


def test_strict_context_guard_holds_access_evidence():
    from core.context_api import _strict_url_project_allowed

    conn = MagicMock()
    with patch("core.admin_api._strict_project_capability_allowed", return_value=True) as guard:
        assert _strict_url_project_allowed(
            conn,
            identity="p1",
            project_id="project_a",
            minimum_capability="edit",
        )

    assert guard.call_args.kwargs["hold_access"] is True


@pytest.mark.parametrize("kind", ["topic", "procedure"])
def test_archived_context_patch_is_explicit_conflict_without_mutation(kind):
    from core.context_store import ArchivedContextError, update_procedure, update_topic

    cursor = MagicMock()
    if kind == "topic":
        cursor.description = [
            ("id",), ("project_id",), ("title",), ("body_md",), ("status",),
            ("owner",), ("created_by",), ("created_at",), ("updated_at",),
        ]
        cursor.fetchone.side_effect = [
            ("top_1", "project_a", "Topic", "Body", "archived", None, "p1", "created", "updated"),
            (2,),
        ]
        invoke = lambda conn: update_topic(  # noqa: E731
            conn,
            topic_id="top_1",
            patch={"body_md": "Changed"},
            changed_by="p1",
            expected_version=2,
        )
        update_sql = "UPDATE app.context_topics"
    else:
        cursor.description = [
            ("id",), ("project_id",), ("name",), ("description",),
            ("frontmatter_yaml",), ("body_md",), ("status",), ("owner",),
            ("created_by",), ("created_at",), ("updated_at",),
        ]
        cursor.fetchone.side_effect = [
            (
                "proc_1", "project_a", "procedure", "Description",
                "name: procedure\ndescription: Description", "Body", "archived",
                None, "p1", "created", "updated",
            ),
            (2,),
        ]
        invoke = lambda conn: update_procedure(  # noqa: E731
            conn,
            procedure_id="proc_1",
            patch={"body_md": "Changed"},
            changed_by="p1",
            expected_version=2,
        )
        update_sql = "UPDATE app.procedures"
    cursor_cm = MagicMock()
    cursor_cm.__enter__.return_value = cursor
    cursor_cm.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cursor_cm

    with pytest.raises(ArchivedContextError):
        invoke(conn)

    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert not any(update_sql in sql for sql in statements)


@pytest.mark.anyio
async def test_topic_versions_pass_caller_project_to_history_query():
    from core.context_api import _list_topic_versions

    history = MagicMock(return_value=[])
    with (
        patch("core.context_api._check_auth", new=AsyncMock(return_value=(True, "p1"))),
        patch("core.context_api._strict_url_project_allowed", return_value=True),
        patch("core.db.get_connection", return_value=nullcontext(MagicMock())),
        patch("core.context_store.get_topic", return_value=_topic()),
        patch("core.context_store.list_topic_versions", history),
    ):
        response = await _list_topic_versions(
            _request(query={"project_id": "project_a"}, path={"id": "top_1"})
        )

    assert response.status_code == 200
    assert history.call_args.kwargs["caller_project_id"] == "project_a"
