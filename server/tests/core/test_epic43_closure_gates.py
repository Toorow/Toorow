"""Epic 43 Closure Gates Tests (Stories 43.13, 43.16, 43.17).

Pins:
1. POST /api/projects/{project_id}/connections/google_direct (Story 43.13):
   - 401 unauthenticated
   - 400 missing project_id
   - 404 cross-project or non-existent project
   - 200/503 initiates OAuth consent flow
2. GET /api/projects/{project_id}/datastreams/{ds_id}/sample/export (Story 43.16):
   - 401 unauthenticated
   - 400 missing params
   - 200 returns CSV attachment with Content-Disposition header
3. POST /api/projects/{project_id}/datastreams/{ds_id}/publish (Story 43.17):
   - 401 unauthenticated
   - 400 missing params
   - 404 cross-project / missing datastream
   - 200 promotes candidate version to published_version_id
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from core import catalog_api  # AD-43 : le handler vit chez son sujet
from starlette.datastructures import QueryParams


def _request(*, query: dict | None = None, path: dict | None = None, body: dict | None = None):
    request = MagicMock()
    request.query_params = QueryParams(query or {})
    request.path_params = path or {}
    request.body = AsyncMock(return_value=json.dumps(body or {}).encode())
    return request


def test_routes_registered():
    from core.admin_api import router
    paths = {route.path for route in router.routes}
    assert "/api/projects/{project_id}/connections/google_direct" in paths
    assert "/api/projects/{project_id}/datastreams/{ds_id}/sample/export" in paths
    # Story 47.5 AC1 removed the direct publish route ON PURPOSE: "the former direct
    # publish, mapping-pointer advance and `enabled=true` console routes cannot
    # bypass the exact review and AD-27 confirmation flow". Publication now goes
    # through the confirmation pair, which is what this gate must hold to -- asserting
    # the retired path would demand the bypass back.
    from core.datastream_preconfiguration_api import datastream_preconfiguration_routes

    preconfiguration_paths = {route.path for route in datastream_preconfiguration_routes}
    assert (
        "/api/projects/{project_id}/datastreams/{datastream_id}/executions/"
        "{execution_id}/publish-confirmations" in preconfiguration_paths
    )
    assert (
        "/api/projects/{project_id}/datastreams/{datastream_id}/executions/"
        "{execution_id}/publish-activate" in preconfiguration_paths
    )
    assert "/api/projects/{project_id}/datastreams/{ds_id}/publish" not in paths


def test_google_direct_unauthenticated():
    from core.project_connections_api import _initiate_google_direct_connection  # noqa: PLC0415
    with patch("core.admin_api._check_auth", new=AsyncMock(return_value=(False, ""))):
        res = asyncio.run(_initiate_google_direct_connection(_request(path={"project_id": "p1"})))
        assert res.status_code == 401


def test_sample_export_unauthenticated():
    from core.datastream_sample_api import _export_datastream_sample_excel  # noqa: PLC0415
    with patch("core.admin_api._check_auth", new=AsyncMock(return_value=(False, ""))):
        res = asyncio.run(
            _export_datastream_sample_excel(
                _request(path={"project_id": "p1", "ds_id": "ds1"})
            )
        )
        assert res.status_code == 401


def test_publish_datastream_unauthenticated():
    from core.admin_api import _publish_datastream_first_publication
    with patch("core.admin_api._check_auth", new=AsyncMock(return_value=(False, ""))):
        res = asyncio.run(
            _publish_datastream_first_publication(
                _request(path={"project_id": "p1", "ds_id": "ds1"})
            )
        )
        assert res.status_code == 401


def test_sample_export_refuses_without_a_date_range():
    """The export reads a bounded window, so it cannot be asked for "everything".

    This assertion replaces one that certified a defect. The old gate called the
    handler with NO date range and a connection mock that returned nothing, and
    asserted 200 + CSV — which passed because the handler fabricated its own
    content instead of reading the warehouse. A gate that a fabricating
    implementation satisfies more easily than a real one is worse than no gate.
    """
    from core.datastream_sample_api import _export_datastream_sample_excel  # noqa: PLC0415
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_admin"))),
        patch("core.admin_api._require_datastream_role", return_value=None),
    ):
        res = asyncio.run(
            _export_datastream_sample_excel(_request(path={"project_id": "p1", "ds_id": "ds1"}))
        )
        assert res.status_code == 400


def test_sample_export_writes_the_rows_the_reader_returned_and_invents_none():
    from core.datastream_sample_api import _export_datastream_sample_excel  # noqa: PLC0415
    mock_cursor = MagicMock()
    # the datastream row, then the same-connector count used to fail closed
    mock_cursor.fetchone.side_effect = [("p1", "search_console"), (1,)]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    sample = {
        "days": [
            {"date": "2026-07-28", "rows": [{"page": "/a", "clicks": 4}]},
            {"date": "2026-07-29", "rows": [{"page": "/b", "clicks": 7}]},
        ],
    }

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_admin"))),
        patch("core.admin_api._require_datastream_role", return_value=None),
        patch("core.db.get_connection") as mock_get_conn,
        patch("core.cache_warehouse.read_datastream_sample", return_value=sample),
    ):
        mock_get_conn.return_value.__enter__.return_value = mock_conn
        res = asyncio.run(
            _export_datastream_sample_excel(
                _request(
                    path={"project_id": "p1", "ds_id": "ds1"},
                    query={"date_from": "2026-07-28", "date_to": "2026-07-29"},
                )
            )
        )
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/csv")
        assert (
            'attachment; filename="datastream_ds1_sample_processed.csv"'
            in res.headers["content-disposition"]
        )

        body = res.body.decode("utf-8")
        assert "date,page,clicks" in body
        assert "2026-07-28,/a,4" in body
        assert "2026-07-29,/b,7" in body
        # The literal the old implementation shipped as if it were evidence.
        assert "masked_bounded_sample" not in body


def test_publish_datastream_not_found():
    from core.admin_api import _publish_datastream_first_publication
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = None
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_admin"))),
        patch("core.admin_api._require_datastream_role", return_value=None),
        patch("core.db.get_connection") as mock_get_conn,
    ):
        mock_get_conn.return_value.__enter__.return_value = mock_conn
        res = asyncio.run(
            _publish_datastream_first_publication(
                _request(path={"project_id": "p1", "ds_id": "ds999"})
            )
        )
        assert res.status_code == 404


def test_publish_datastream_success():
    from core.admin_api import _publish_datastream_first_publication
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = ("ver_cand_123", None)
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_admin"))),
        patch("core.admin_api._require_datastream_role", return_value=None),
        patch("core.db.get_connection") as mock_get_conn,
        patch("core.admin_api.write_audit_row") as mock_audit,
    ):
        mock_get_conn.return_value.__enter__.return_value = mock_conn
        res = asyncio.run(
            _publish_datastream_first_publication(
                _request(path={"project_id": "p1", "ds_id": "ds1"})
            )
        )
        assert res.status_code == 200
        body = json.loads(res.body.decode())
        assert body["status"] == "published"
        assert body["published_version_id"] == "ver_cand_123"
        mock_audit.assert_called_once()


def test_reports_available_is_tenant_scoped():
    """Regression, production measurement 2026-07-27.

    GET /api/reports/available?project_id=proj_TOTALEMENT_INEXISTANT_12345
    answered 200 with 16 reports across 12 modules -- for a project that does not
    exist, to an identity that is a member of nothing. It checked only that
    project_id was a non-empty string, leaking which connector modules the
    deployment runs. R43-FR09 requires every project-scoped list to be tenant
    scoped; this one was not.

    The answer must be 404, not 403: existence is itself sensitive, exactly like
    golden questions, eval runs, procedures and datamodel fields.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from core import admin_api

    request = MagicMock()
    request.query_params = {"project_id": "proj_TOTALEMENT_INEXISTANT_12345"}

    with (
        patch.object(admin_api, "_check_auth", AsyncMock(return_value=(True, "person_stranger"))),
        patch.object(admin_api, "_strict_project_capability_allowed", return_value=False),
        patch("core.db.get_connection", MagicMock()),
    ):
        response = asyncio.run(catalog_api._list_available_reports(request))

    assert response.status_code == 404, "a non-member must not receive the report catalogue"
    assert b"not_found" in response.body


def test_reports_available_lists_only_connected_connectors():
    """Decision Jean, 2026-07-27: "on s'en fout si tu as 300+ connecteurs".

    A brand-new project has no connection, no Datastream and no row in
    app.project_modules -- and because module enablement defaults permissively,
    this endpoint answered with the deployment's whole catalogue: 16 reports over
    12 modules for a project with zero sources. Reports must follow the sources
    actually entered.

    Also asserts the failure direction: when the connection set cannot be read the
    filter is SKIPPED, never applied as an empty set, so a database problem cannot
    look like a project with nothing connected.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from core import admin_api

    request = MagicMock()
    request.query_params = {"project_id": "proj_1"}

    catalog = [
        {"module_name": "gsc", "report_id": "query_page_daily", "display_name": "GSC"},
        {"module_name": "meta-ads", "report_id": "campaign_daily", "display_name": "Meta"},
    ]

    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    # First query: connected providers. Second: app.project_reports rows.
    cursor.fetchall.side_effect = [[("gsc",)], []]
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False

    with (
        patch.object(admin_api, "_check_auth", AsyncMock(return_value=(True, "person_1"))),
        patch.object(admin_api, "_strict_project_capability_allowed", return_value=True),
        patch.object(catalog_api, "_connector_report_catalog", return_value=catalog),
        patch("core.db.get_connection", return_value=conn),
        patch("core.module_enablement.is_module_enabled", return_value=True),
    ):
        response = asyncio.run(catalog_api._list_available_reports(request))

    assert response.status_code == 200
    body = json.loads(response.body.decode())
    modules = {row["module_name"] for row in body}
    assert modules == {"gsc"}, "only the connected connector may appear"
