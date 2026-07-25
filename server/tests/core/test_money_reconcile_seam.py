"""AI-56 seam tests for POST /api/money/reconcile via build_asgi_app (Story 39.6).

Exercises the NEW reconcile route through the REAL ASGI stack (mirrors
test_money_api_seam.py for /api/money/aggregation-check):
  - Route is mounted (401 without token / not 404).
  - 422 on a malformed body (missing metric / non-list contributions).
  - 404 on cross-project (AD-5 guard, non-disclosant).
  - 200 {reconciled:true, result:{...}} on a legal same-currency reconcile.
  - 200 {reconciled:false, refusal:{...}} on an unknown-currency contribution
    (a refusal is an HONEST 200, not a 4xx).

Postgres is mocked (reporting-currency read + project guard patched); is_monetary is patched.
"""

from __future__ import annotations

import os

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

_ROUTE = "/api/money/reconcile"


@pytest.fixture()
def client():
    """Full ASGI app client with auth + project guard mocked; reporting ccy patched -> EUR."""
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    app = build_asgi_app()
    with patch(
        "core.money_api._check_auth",
        new=AsyncMock(return_value=(True, "seam@test")),
    ), patch(
        "core.money_api._guard_project_access",
        new=AsyncMock(return_value=True),
    ), patch(
        "core.money_api._resolve_reporting_currency",
        return_value="EUR",
    ), patch(
        "core.db.get_connection",
    ) as mock_gc:
        mock_conn = MagicMock()
        mock_gc.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_gc.return_value.__exit__ = MagicMock(return_value=False)
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


@pytest.fixture()
def client_unauth():
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    app = build_asgi_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _monetary(value):
    return patch("core.money_api._resolve_is_monetary", return_value=value)


# ---------------------------------------------------------------------------
# Route mounting
# ---------------------------------------------------------------------------


def test_route_mounted_without_token(client_unauth):
    resp = client_unauth.post(_ROUTE, json={"metric": "revenue", "contributions": []})
    assert resp.status_code != 404, "reconcile route not spliced into admin_api"
    assert resp.status_code in (401, 500)


# ---------------------------------------------------------------------------
# Malformed body -> 422
# ---------------------------------------------------------------------------


def test_missing_metric_returns_422(client):
    resp = client.post(_ROUTE, json={"project_id": "proj_a", "contributions": []})
    assert resp.status_code == 422


def test_contributions_not_a_list_returns_422(client):
    resp = client.post(
        _ROUTE, json={"project_id": "proj_a", "metric": "revenue", "contributions": "nope"}
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Cross-project -> 404 (AD-5)
# ---------------------------------------------------------------------------


def test_cross_project_returns_404(client_unauth):
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    app = build_asgi_app()
    with patch(
        "core.money_api._check_auth",
        new=AsyncMock(return_value=(True, "seam@test")),
    ), patch(
        "core.money_api._guard_project_access",
        new=AsyncMock(return_value=False),
    ), patch(
        "core.db.get_connection",
    ) as mock_gc:
        mock_conn = MagicMock()
        mock_gc.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_gc.return_value.__exit__ = MagicMock(return_value=False)
        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.post(
                _ROUTE,
                json={"project_id": "proj_other", "metric": "revenue", "contributions": []},
            )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Legal same-currency reconcile -> 200 {reconciled:true, result:{...}}
# ---------------------------------------------------------------------------


def test_same_currency_reconciles(client):
    body = {
        "project_id": "proj_a",
        "metric": "revenue",
        "contributions": [
            {"connector": "a", "amount": 100.0, "currency": "EUR"},
            {"connector": "b", "amount": 40.0, "currency": "EUR"},
        ],
    }
    fake_route = MagicMock()
    fake_route.status = "DIRECT_SUM"
    fake_route.method = "SUM"
    fake_route.scope_level = None
    fake_route.target_mart = None
    fake_route.priority_order = ()
    fake_route.series = ()
    fake_route.reason = None
    with _monetary(True), patch(
        "core.metric_reconciliation.resolve_route", return_value=fake_route
    ):
        resp = client.post(_ROUTE, json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["reconciled"] is True
    assert data["result"]["status"] == "RECONCILED"
    assert data["result"]["amount"] == 140.0
    assert data["result"]["reporting_currency"] == "EUR"


# ---------------------------------------------------------------------------
# Unknown currency -> 200 {reconciled:false, refusal:{...}} (honest 200)
# ---------------------------------------------------------------------------


def test_unknown_currency_returns_refusal(client):
    body = {
        "project_id": "proj_a",
        "metric": "revenue",
        "contributions": [
            {"connector": "a", "amount": 10.0, "currency": None, "source_system": "s1"},
        ],
    }
    with _monetary(True):
        resp = client.post(_ROUTE, json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["reconciled"] is False
    assert data["refusal"]["code"] == "UNKNOWN_CURRENCY_GAP"
