"""Route tests for GET /api/reports/{module}/{report_id}/chain (Story 8.9).

Mounts REPORT_CHAIN_ROUTES on a test Starlette app and exercises the handler
via TestClient. Auth and chain computation are patched.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from core.report_chain import REPORT_CHAIN_ROUTES
from starlette.routing import Router
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    """Test client with auth always passing as 'test@test'."""
    app = Router(routes=REPORT_CHAIN_ROUTES)
    with patch(
        "core.report_chain._check_auth",
        new=AsyncMock(return_value=(True, "test@test")),
    ):
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


@pytest.fixture()
def client_unauth():
    """Test client that always fails auth."""
    app = Router(routes=REPORT_CHAIN_ROUTES)
    with patch(
        "core.report_chain._check_auth",
        new=AsyncMock(return_value=(False, "")),
    ):
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


# ---------------------------------------------------------------------------
# Sample chain document
# ---------------------------------------------------------------------------


_SAMPLE_CHAIN = {
    "report_id": "google-search-console/overview_daily",
    "display_name": "Vue d'ensemble quotidienne",
    "metric_definitions": None,
    "llm_commentary_guidelines": None,
    "metrics": [
        {
            "metric": "clicks",
            "definition": None,
            "target_field": {
                "name": "clicks",
                "display_name": "Clics",
                "measure": "sum",
                "data_type": "integer",
            },
            "datastreams": [
                {
                    "id": "ds_001",
                    "name": "GSC Stream",
                    "module": "google-search-console",
                    "enabled": True,
                    "last_extract": {"date": "2026-07-10", "status": "ok"},
                }
            ],
            "status": "ok",
        }
    ],
    "validation": {
        "ok_count": 1,
        "warnings": [],
    },
}


# ---------------------------------------------------------------------------
# Route tests
# ---------------------------------------------------------------------------


class TestReportChainRoute:
    def test_401_when_unauthorized(self, client_unauth):
        resp = client_unauth.get(
            "/api/reports/google-search-console/overview_daily/chain?project_id=proj_001"
        )
        assert resp.status_code == 401
        assert resp.json()["code"] == "unauthorized"

    def test_400_when_no_project_id(self, client):
        # No DB needed -- the 400 is returned before any DB access
        resp = client.get(
            "/api/reports/google-search-console/overview_daily/chain"
        )
        assert resp.status_code == 400
        assert "project_id" in resp.json()["message"]

    def _conn_ctx(self):
        """Helper: a mock context manager that yields a MagicMock connection."""
        conn = MagicMock()
        ctx = MagicMock()
        ctx.__enter__ = lambda s: conn
        ctx.__exit__ = MagicMock(return_value=False)
        return ctx

    def test_404_when_chain_is_none(self, client):
        with (
            patch("core.db.get_connection", return_value=self._conn_ctx()),
            patch("core.report_chain.get_report_chain", return_value=None),
            # project_access check: skip by patching it away
            patch(
                "core.project_access.identity_has_project_access",
                return_value=True,
                create=True,
            ),
        ):
            resp = client.get(
                "/api/reports/unknown_module/nonexistent/chain?project_id=proj_001"
            )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_200_returns_chain(self, client):
        with (
            patch("core.db.get_connection", return_value=self._conn_ctx()),
            patch("core.report_chain.get_report_chain", return_value=_SAMPLE_CHAIN),
            patch(
                "core.project_access.identity_has_project_access",
                return_value=True,
                create=True,
            ),
        ):
            resp = client.get(
                "/api/reports/google-search-console/overview_daily/chain?project_id=proj_001"
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["report_id"] == "google-search-console/overview_daily"
        assert len(data["metrics"]) == 1
        assert data["metrics"][0]["status"] == "ok"
        assert data["validation"]["ok_count"] == 1

    def test_200_with_warning_metrics(self, client):
        """Chain with no_stream and not_in_dictionary warnings returns 200."""
        chain_with_warnings = {
            **_SAMPLE_CHAIN,
            "metrics": [
                {
                    "metric": "average_position",
                    "definition": None,
                    "target_field": {
                        "name": "average_position",
                        "display_name": "Position moyenne",
                        "measure": "average",
                        "data_type": "decimal",
                    },
                    "datastreams": [],
                    "status": "no_stream",
                },
                {
                    "metric": "cost_per_click",
                    "definition": None,
                    "target_field": None,
                    "datastreams": [],
                    "status": "not_in_dictionary",
                },
            ],
            "validation": {
                "ok_count": 0,
                "warnings": [
                    "Aucun flux actif n'alimente « Position moyenne » pour ce projet.",
                    "La métrique « cost_per_click » n'est pas référencée dans le dictionnaire.",
                ],
            },
        }

        with (
            patch("core.db.get_connection", return_value=self._conn_ctx()),
            patch("core.report_chain.get_report_chain", return_value=chain_with_warnings),
            patch(
                "core.project_access.identity_has_project_access",
                return_value=True,
                create=True,
            ),
        ):
            resp = client.get(
                "/api/reports/gsc/pos_report/chain?project_id=proj_001"
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["validation"]["ok_count"] == 0
        assert len(data["validation"]["warnings"]) == 2
        statuses = {m["metric"]: m["status"] for m in data["metrics"]}
        assert statuses["average_position"] == "no_stream"
        assert statuses["cost_per_click"] == "not_in_dictionary"

    def test_422_when_report_id_is_chain(self, client):
        """AI-47: report_id == 'chain' must return 422 with code='reserved_id'.

        The route pattern /api/reports/{module}/{report_id}/chain would match a
        report literally named 'chain' as the {report_id} segment, but returning a
        report named 'chain' from the /chain route is not the intent.  The guard
        returns 422 before any DB access so no mock is needed.
        """
        resp = client.get(
            "/api/reports/google-search-console/chain/chain?project_id=proj_001"
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["code"] == "reserved_id"
        # Message must be in French and mention 'chain'
        assert "chain" in body["message"]

    def test_422_chain_guard_fires_before_db(self, client):
        """AI-47: the 422 fires before any DB access (no project_id check, no chain computation)."""
        # Confirm that even with a valid project_id the guard fires immediately
        # and no DB / module-loading is attempted.
        with patch("core.db.get_connection") as mock_conn:
            resp = client.get(
                "/api/reports/my-module/chain/chain?project_id=proj_001"
            )
        assert resp.status_code == 422
        mock_conn.assert_not_called()
