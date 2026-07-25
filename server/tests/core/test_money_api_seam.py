"""AI-56 seam tests for POST /api/money/aggregation-check via build_asgi_app (Story 39.3).

Uses the full ASGI app (AI-56 pattern, mirrors test_conflict_resolutions_seam.py) to verify:
  - Route is mounted (401 without token, not 404/405).
  - Cross-currency body -> 200 {refused:true, refusal:{...EUR,USD, metric}}.
  - Unknown-currency body -> 200 {refused:true, refusal:{code:"UNKNOWN_CURRENCY_GAP"}}.
  - Single-currency body -> 200 {refused:false, result_currency:"EUR"}.
  - Cross-project project_id -> 404 (non-disclosant).
  - Malformed body (missing metric / contributions) -> 422.
  - Non-monetary metric with mixed currencies -> refused:false (engine no-ops).

Postgres is mocked (the reporting-currency read and project guard are patched).
"""

from __future__ import annotations

import os

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

_ROUTE = "/api/money/aggregation-check"


@pytest.fixture()
def client():
    """Full ASGI app client with auth + project guard mocked; reporting ccy + is_monetary
    resolvers patched so no DB is required."""
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
    """Full ASGI app client that always returns 401 (no auth patch)."""
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    app = build_asgi_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _monetary(value):
    return patch("core.money_api._resolve_is_monetary", return_value=value)


# ---------------------------------------------------------------------------
# Route mounting (22)
# ---------------------------------------------------------------------------


def test_route_mounted_without_token(client_unauth):
    # Env-independent seam assertion (mirror test_conflict_resolutions_seam): a MOUNTED route
    # resolves to its handler -- auth 401, or 500 when auth is disabled and no DB is reachable.
    # An UNMOUNTED route returns 404. So status != 404 proves the splice, regardless of env.
    resp = client_unauth.post(_ROUTE, json={"metric": "revenue", "contributions": []})
    assert resp.status_code != 404, "route not spliced into admin_api"
    assert resp.status_code in (401, 500)


# ---------------------------------------------------------------------------
# Cross-currency (23)
# ---------------------------------------------------------------------------


def test_cross_currency_returns_refusal(client):
    body = {
        "project_id": "proj_a",
        "metric": "revenue",
        "contributions": [
            {"amount": 10, "currency": "USD", "source_system": "s1",
             "source_field": "f", "pull_id": "p1"},
            {"amount": 20, "currency": "EUR", "source_system": "s2",
             "source_field": "f", "pull_id": "p2"},
        ],
    }
    with _monetary(True):
        resp = client.post(_ROUTE, json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["refused"] is True
    refusal = data["refusal"]
    assert refusal["code"] == "CROSS_CURRENCY_REFUSAL"
    assert refusal["metric"] == "revenue"
    assert refusal["conflicting_currencies"] == ["EUR", "USD"]


# ---------------------------------------------------------------------------
# Unknown currency (24)
# ---------------------------------------------------------------------------


def test_unknown_currency_returns_gap(client):
    body = {
        "project_id": "proj_a",
        "metric": "cost",
        "contributions": [
            {"amount": 10, "currency": None, "source_system": "s1",
             "source_field": "f", "pull_id": "p1"},
        ],
    }
    with _monetary(True):
        resp = client.post(_ROUTE, json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["refused"] is True
    assert data["refusal"]["code"] == "UNKNOWN_CURRENCY_GAP"


# ---------------------------------------------------------------------------
# Single currency legal (25)
# ---------------------------------------------------------------------------


def test_single_currency_not_refused(client):
    body = {
        "project_id": "proj_a",
        "metric": "revenue",
        "contributions": [
            {"amount": 10, "currency": "EUR", "source_system": "s1",
             "source_field": "f", "pull_id": "p1"},
            {"amount": 20, "currency": "EUR", "source_system": "s2",
             "source_field": "f", "pull_id": "p2"},
        ],
    }
    with _monetary(True):
        resp = client.post(_ROUTE, json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["refused"] is False
    assert data["result_currency"] == "EUR"


# ---------------------------------------------------------------------------
# Cross-project 404 (26)
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
        new=AsyncMock(return_value=False),  # deny access
    ), patch(
        "core.db.get_connection",
    ) as mock_gc:
        mock_conn = MagicMock()
        mock_gc.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_gc.return_value.__exit__ = MagicMock(return_value=False)
        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.post(
                _ROUTE,
                json={
                    "project_id": "proj_other",
                    "metric": "revenue",
                    "contributions": [],
                },
            )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Malformed body 422 (27)
# ---------------------------------------------------------------------------


def test_missing_metric_returns_422(client):
    resp = client.post(_ROUTE, json={"project_id": "proj_a", "contributions": []})
    assert resp.status_code == 422


def test_missing_contributions_returns_422(client):
    resp = client.post(_ROUTE, json={"project_id": "proj_a", "metric": "revenue"})
    assert resp.status_code == 422


def test_contributions_not_a_list_returns_422(client):
    resp = client.post(
        _ROUTE,
        json={"project_id": "proj_a", "metric": "revenue", "contributions": "nope"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Non-monetary metric no-ops (28)
# ---------------------------------------------------------------------------


def test_non_monetary_metric_not_refused(client):
    body = {
        "project_id": "proj_a",
        "metric": "clicks",
        "contributions": [
            {"amount": 10, "currency": "EUR", "source_system": "s1",
             "source_field": "f", "pull_id": "p1"},
            {"amount": 20, "currency": "USD", "source_system": "s2",
             "source_field": "f", "pull_id": "p2"},
        ],
    }
    with _monetary(False):
        resp = client.post(_ROUTE, json=body)
    assert resp.status_code == 200
    assert resp.json()["refused"] is False
