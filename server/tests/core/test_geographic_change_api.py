from __future__ import annotations

from core import admin_api
from core.geographic_change import GeographicPreviewStale
from starlette.testclient import TestClient


async def _authorized(_request):
    return True, "operator@example.test"


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_preview_route_requires_idempotency_key(monkeypatch) -> None:
    monkeypatch.setattr(admin_api, "_check_auth", _authorized)
    response = TestClient(admin_api.router).post(
        "/api/projects/project-1/geography/preview",
        json={"geographic_mode": "local_markets", "local_market_country_codes": ["FR"]},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "missing_idempotency_key"


def test_preview_route_returns_complete_impact(monkeypatch) -> None:
    monkeypatch.setattr(admin_api, "_check_auth", _authorized)
    monkeypatch.setattr("core.db.get_connection", lambda: _Connection())
    monkeypatch.setattr(admin_api, "_require_datastream_role", lambda *_args: None)
    monkeypatch.setattr("core.main.get_loaded_modules", lambda: [])
    captured = []

    def fake_preview(**kwargs):
        captured.append(kwargs)
        return {
            "id": "gprev-1",
            "impact": {
                "affected_datastream_count": 1,
                "blocking_gap_count": 0,
                "datastreams": [],
            },
            "idempotent_replay": False,
        }

    monkeypatch.setattr("core.geographic_change.create_geographic_change_preview", fake_preview)
    response = TestClient(admin_api.router).post(
        "/api/projects/project-1/geography/preview",
        headers={"Idempotency-Key": "preview-key"},
        json={"geographic_mode": "local_markets", "local_market_country_codes": ["FR"]},
    )

    assert response.status_code == 201
    assert response.json()["id"] == "gprev-1"
    assert captured[0]["target"].country_codes == ("FR",)
    assert captured[0]["idempotency_key"] == "preview-key"


def test_confirm_route_maps_stale_preview_to_conflict(monkeypatch) -> None:
    monkeypatch.setattr(admin_api, "_check_auth", _authorized)
    monkeypatch.setattr("core.db.get_connection", lambda: _Connection())
    monkeypatch.setattr(admin_api, "_require_datastream_role", lambda *_args: None)
    monkeypatch.setattr("core.main.get_loaded_modules", lambda: [])

    def stale(**_kwargs):
        raise GeographicPreviewStale("fresh preview required")

    monkeypatch.setattr("core.geographic_change.confirm_geographic_change", stale)
    response = TestClient(admin_api.router).post(
        "/api/projects/project-1/geography/previews/gprev-1/confirm",
        json={"backfill_decision": "defer"},
    )

    assert response.status_code == 409
    assert response.json() == {
        "code": "geographic_preview_stale",
        "message": "fresh preview required",
    }


def test_preview_and_confirm_routes_are_registered_before_project_catchall() -> None:
    paths = [route.path for route in admin_api.router.routes]
    assert "/api/projects/{project_id}/geography/preview" in paths
    assert "/api/projects/{project_id}/geography/previews/{preview_id}/confirm" in paths
