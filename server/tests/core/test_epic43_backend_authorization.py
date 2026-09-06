"""Focused authorization coverage for Epic 43.18-43.20 backend evidence routes."""

from __future__ import annotations

import json
from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.datastructures import QueryParams


def _request(*, query: dict | None = None, path: dict | None = None, body: dict | None = None):
    request = MagicMock()
    request.query_params = QueryParams(query or {})
    request.path_params = path or {}
    request.body = AsyncMock(return_value=json.dumps(body or {}).encode())
    return request


def _connection_for_rows(*rows):
    conn = MagicMock()
    cursors = []
    for row in rows:
        cursor = MagicMock()
        cursor.fetchone.return_value = row
        cursor_cm = MagicMock()
        cursor_cm.__enter__.return_value = cursor
        cursor_cm.__exit__.return_value = False
        cursors.append(cursor_cm)
    conn.cursor.side_effect = cursors
    return conn


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        ((None,), "foreign project"),
        ((("org_a", "active", None, None),), "unenrolled identity"),
        ((("org_a", "active", "member", "active"), None), "missing project grant"),
    ],
)
def test_strict_app_guard_denies_foreign_unenrolled_and_ungranted(
    monkeypatch, rows, reason
):
    from core.admin_api import _strict_project_capability_allowed

    monkeypatch.setenv("TOOROW_AUTH_MODE", "token")
    conn = _connection_for_rows(*rows)

    assert not _strict_project_capability_allowed(
        conn,
        identity="person_a",
        project_id="project_a",
        minimum_capability="view",
    ), reason


def test_strict_app_guard_allows_exact_grant(monkeypatch):
    from core.admin_api import _strict_project_capability_allowed

    monkeypatch.setenv("TOOROW_AUTH_MODE", "token")
    conn = _connection_for_rows(
        ("org_a", "active", "member", "active"),
        ("edit",),
    )

    assert _strict_project_capability_allowed(
        conn,
        identity="person_a",
        project_id="project_a",
        minimum_capability="edit",
    )


def test_strict_app_guard_fails_closed_when_access_database_fails(monkeypatch):
    from core.admin_api import _strict_project_capability_allowed

    monkeypatch.setenv("TOOROW_AUTH_MODE", "token")
    conn = MagicMock()
    conn.cursor.side_effect = RuntimeError("secret database detail")

    assert not _strict_project_capability_allowed(
        conn,
        identity="person_a",
        project_id="project_a",
        minimum_capability="view",
    )


def test_auth_disabled_anonymous_compatibility_is_explicit(monkeypatch):
    """The bypass grants every CAPABILITY -- and asks the database exactly ONE
    question: does this project exist?

    It used to ask nothing at all, which is what `conn.cursor.assert_not_called()`
    pinned here. That made `make dev` grant `manage` on every string, so
    `?project_id=proj_TYPO` passed the gate and five Context Hub reads answered
    `200` with an empty list -- an invented project that a console renders as an
    empty one (live finding C1). Existence is not authorization, and skipping the
    authorization check is not a licence to skip the lookup.
    """
    from core.admin_api import _strict_project_capability_allowed

    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    conn = MagicMock()

    assert _strict_project_capability_allowed(
        conn,
        identity="anonymous",
        project_id="local_project",
        minimum_capability="manage",
    )
    # ONE query, and it is the existence probe -- not the strict resolver, which
    # would deny on `production_identity_required` and break the local workflow.
    sql = conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0]
    assert "FROM app.projects" in sql
    # AI-363 (2026-09-02, `project-settings.md`): an archived project stays
    # visible to its managers, so the probe admits a SET of statuses bound by
    # the caller rather than the literal `'active'` this line pinned before.
    assert "status = ANY(%s)" in sql

    # And a project that does NOT exist is refused, in this mode as in the other.
    absent = MagicMock()
    absent.cursor.return_value.__enter__.return_value.fetchone.return_value = None
    assert not _strict_project_capability_allowed(
        absent,
        identity="anonymous",
        project_id="proj_TYPO",
        minimum_capability="view",
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("handler_name", "request_value", "capability"),
    [
        (
            "legacy_evidence_api._list_legacy_feedback_rows",
            _request(query={"project_id": "project_a"}),
            "view",
        ),
        # `_list_tracked_entities` was here. `GET /api/tracked-entities` was
        # REMOVED by Story 48.5 ("the registry becomes a lens someone can reach,
        # and twelve tools stop existing") and the handler went with it --
        # `core/admin_api.py#tracked-entities` records the removal in place. A parametrize
        # entry naming a handler that no longer exists cannot prove a
        # non-disclosing 404; it just errors. Removed rather than xfailed: the
        # surface is gone, so there is no guarantee left to pin here.
        (
            "legacy_evidence_api._list_legacy_benchmark_questions",
            _request(query={"project_id": "project_a"}),
            "view",
        ),
        (
            "legacy_evidence_api._list_legacy_eval_runs",
            _request(query={"project_id": "project_a"}),
            "view",
        ),
        # AD-43 : le module est NOMME, pas cherche. Deux fonctions `_list_notebooks`
        # existent -- `analyze_artifacts_api` en a une autre -- et une recherche par
        # nom prend parfois la bonne par accident, ce qui est pire que de se tromper.
        ("notebooks_api._list_notebooks", _request(query={"project_id": "project_a"}), "view"),
        (
            "catalog_api._patch_report",
            _request(
                path={
                    "project_id": "project_a",
                    "connector_name": "connector_a",
                    "report_id": "report_a",
                },
                body={"enabled": True},
            ),
            "edit",
        ),
    ],
)
async def test_admin_surfaces_return_nondisclosing_404_before_domain_access(
    handler_name, request_value, capability
):
    # AD-43 : les handlers ont rejoint le module de leur SUJET, donc chaque
    # entree nomme `module.handler`. Une garantie appartient au handler, pas au
    # fichier ou il se trouvait -- et deux modules peuvent porter le meme nom de
    # fonction, ce qu'une recherche par nom seul resout parfois par accident.
    import importlib

    module_name, _, attribute = handler_name.rpartition(".")
    module = importlib.import_module(f"core.{module_name}")
    handler = getattr(module, attribute, None)
    assert handler is not None, (
        f"core.{module_name} ne porte plus {attribute} -- s'il a demenage, cette "
        "entree suit le handler ; s'il a ete supprime, la garantie n'a plus de titulaire"
    )
    conn = MagicMock()
    guard = MagicMock(return_value=False)
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_a"))),
        patch("core.admin_api._strict_project_capability_allowed", guard),
        patch("core.db.get_connection", return_value=nullcontext(conn)),
    ):
        response = await handler(request_value)

    assert response.status_code == 404
    assert json.loads(response.body) == {
        "code": "not_found",
        "message": "Project not found",
    }
    assert guard.call_args.kwargs["minimum_capability"] == capability
    conn.cursor.assert_not_called()


@pytest.mark.anyio
async def test_report_patch_accepts_member_edit_grant(monkeypatch):
    from core.catalog_api import _patch_report  # noqa: PLC0415

    monkeypatch.setenv("TOOROW_AUTH_MODE", "token")
    access_conn = _connection_for_rows(
        ("org_a", "active", "member", "active"),
        ("edit",),
    )
    mutation_cursor = MagicMock()
    mutation_cursor.fetchone.return_value = (
        "project_a",
        "module_a",
        "report_a",
        True,
        0,
    )
    mutation_cm = MagicMock()
    mutation_cm.__enter__.return_value = mutation_cursor
    mutation_cm.__exit__.return_value = False
    mutation_conn = MagicMock()
    mutation_conn.cursor.return_value = mutation_cm

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_a"))),
        patch(
            "core.db.get_connection",
            side_effect=[nullcontext(access_conn), nullcontext(mutation_conn)],
        ),
    ):
        response = await _patch_report(
            _request(
                path={
                    "project_id": "project_a",
                    "connector_name": "connector_a",
                    "report_id": "report_a",
                },
                body={"enabled": True},
            )
        )

    assert response.status_code == 200
    assert json.loads(response.body)["enabled"] is True
    mutation_conn.commit.assert_called_once_with()

@pytest.mark.anyio
async def test_admin_access_lookup_exception_is_nondisclosing():
    from core.legacy_evidence_api import _list_legacy_eval_runs

    request = _request(query={"project_id": "project_a"})
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_a"))),
        patch(
            "core.admin_api._strict_project_capability_allowed",
            side_effect=RuntimeError("database hostname and password"),
        ),
        patch("core.db.get_connection", return_value=nullcontext(MagicMock())),
    ):
        response = await _list_legacy_eval_runs(request)

    assert response.status_code == 404
    assert "database" not in response.body.decode().lower()
    assert "password" not in response.body.decode().lower()


@pytest.mark.anyio
async def test_admin_missing_auth_returns_401_before_access_lookup():
    from core.legacy_evidence_api import _list_legacy_benchmark_questions

    guard = MagicMock()
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(False, ""))),
        patch("core.admin_api._strict_project_capability_allowed", guard),
    ):
        response = await _list_legacy_benchmark_questions(
            _request(query={"project_id": "project_a"})
        )

    assert response.status_code == 401
    guard.assert_not_called()


@pytest.mark.anyio
async def test_golden_questions_allowed_access_returns_persisted_rows():
    from core.legacy_evidence_api import _list_legacy_benchmark_questions

    cursor = MagicMock()
    cursor.description = [
        ("id",),
        ("question",),
        ("topic",),
        ("expected_citations",),
        ("last_result",),
    ]
    cursor.fetchall.return_value = [
        ("gold_1", "What changed?", "trust", ["publication"], "pass")
    ]
    cursor_cm = MagicMock()
    cursor_cm.__enter__.return_value = cursor
    cursor_cm.__exit__.return_value = False
    domain_conn = MagicMock()
    domain_conn.cursor.return_value = cursor_cm

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_a"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=True),
        patch(
            "core.db.get_connection",
            side_effect=[nullcontext(MagicMock()), nullcontext(domain_conn)],
        ),
    ):
        response = await _list_legacy_benchmark_questions(
            _request(query={"project_id": "project_a"})
        )

    assert response.status_code == 200
    assert json.loads(response.body)["questions"][0]["id"] == "gold_1"


@pytest.mark.anyio
async def test_datamodel_field_list_denies_before_store_query():
    from core.datamodel_api import _list_fields

    store = MagicMock()
    with (
        patch("core.datamodel_api._check_auth", new=AsyncMock(return_value=(True, "person_a"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=False),
        patch("core.db.get_connection", return_value=nullcontext(MagicMock())),
        patch("core.datamodel.list_target_fields", store),
    ):
        response = await _list_fields(_request(query={"project_id": "project_a"}))

    assert response.status_code == 404
    store.assert_not_called()


@pytest.mark.anyio
async def test_datamodel_field_list_allows_view_grant():
    from core.datamodel_api import _list_fields

    with (
        patch("core.datamodel_api._check_auth", new=AsyncMock(return_value=(True, "person_a"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=True),
        patch(
            "core.db.get_connection",
            side_effect=[nullcontext(MagicMock()), nullcontext(MagicMock())],
        ),
        patch("core.datamodel.list_target_fields", return_value=[]),
    ):
        response = await _list_fields(_request(query={"project_id": "project_a"}))

    assert response.status_code == 200
    assert json.loads(response.body) == []


@pytest.mark.anyio
@pytest.mark.parametrize("minimum_capability", ["view", "edit"])
async def test_conflict_guard_delegates_exact_capability(minimum_capability):
    from core.conflict_resolutions_api import _guard_project_access

    shared_guard = MagicMock(return_value=True)
    with patch(
        "core.admin_api._strict_project_capability_allowed",
        shared_guard,
    ):
        allowed = await _guard_project_access(
            "project_a",
            "person_a",
            MagicMock(),
            minimum_capability=minimum_capability,
        )

    assert allowed
    assert shared_guard.call_args.kwargs["minimum_capability"] == minimum_capability


@pytest.mark.anyio
async def test_conflict_guard_database_failure_denies_without_disclosure():
    from core.conflict_resolutions_api import _guard_project_access

    with patch(
        "core.admin_api._strict_project_capability_allowed",
        side_effect=RuntimeError("secret database detail"),
    ):
        allowed = await _guard_project_access(
            "project_a",
            "person_a",
            MagicMock(),
            minimum_capability="view",
        )

    assert not allowed

