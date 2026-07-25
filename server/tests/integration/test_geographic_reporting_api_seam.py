"""build_asgi_app seams for Story 37.1 project geography contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from core.main import build_asgi_app
from starlette.testclient import TestClient


class _Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = []
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.connection.executed.append((normalized, params))
        self.row = None
        if normalized.startswith("SELECT geographic_mode"):
            self.row = (
                self.connection.geographic_mode,
                list(self.connection.country_codes),
            )
        elif normalized.startswith("SELECT COUNT(*) FROM app.datastreams"):
            self.row = (self.connection.governed_count,)
        elif normalized.startswith("SELECT verification_source_type"):
            self.row = (None, None, None)
        elif normalized.startswith("SELECT id, name, slug, status, currency, timezone"):
            now = datetime(2026, 7, 22, tzinfo=timezone.utc)
            self.row = (
                "proj_geo",
                "Geo project",
                "geo-project",
                "active",
                "EUR",
                "Europe/Paris",
                now,
                now,
            )
            self.description = [
                ("id",),
                ("name",),
                ("slug",),
                ("status",),
                ("currency",),
                ("timezone",),
                ("created_at",),
                ("updated_at",),
            ]
        if normalized.startswith("INSERT INTO app.project_preferences"):
            self.connection.geographic_mode = params[1]
            self.connection.country_codes = list(params[2])

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self):
        self.geographic_mode = "global"
        self.country_codes = []
        self.governed_count = 0
        self.executed = []
        self.committed = False
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def test_project_patch_and_read_geography_through_build_asgi_app(monkeypatch):
    connection = _Connection()
    monkeypatch.setattr("core.db.get_connection", lambda: connection)
    monkeypatch.setattr(
        "core.project_access.identity_can_access_project_in_org",
        lambda project_id, identity, conn: True,
    )

    app = build_asgi_app()
    with patch("core.admin_api._check_auth", return_value=(True, "operator@test")):
        patch_response = TestClient(app).patch(
            "/api/projects/proj_geo",
            json={
                "geographic_mode": "local_markets",
                "local_market_country_codes": ["FR", "DE"],
            },
        )
        get_response = TestClient(app).get("/api/projects/proj_geo")

    assert patch_response.status_code == 200
    assert patch_response.json()["geographic_mode"] == "local_markets"
    assert patch_response.json()["local_market_country_codes"] == ["DE", "FR"]
    assert get_response.status_code == 200
    assert get_response.json()["local_market_country_codes"] == ["DE", "FR"]
    assert connection.committed is True

    audit_params = next(
        params for sql, params in connection.executed if sql.startswith("INSERT INTO app.audit_log")
    )
    assert audit_params[1] == "operator@test"
    assert audit_params[2] == "project.geographic_posture.updated"
    assert '"geographic_mode": "global"' in audit_params[5]
    assert '"geographic_mode": "local_markets"' in audit_params[5]


def test_direct_geographic_patch_requires_preview_for_governed_datastreams(monkeypatch):
    connection = _Connection()
    connection.governed_count = 1
    monkeypatch.setattr("core.db.get_connection", lambda: connection)
    monkeypatch.setattr(
        "core.project_access.identity_can_access_project_in_org",
        lambda project_id, identity, conn: True,
    )

    with patch("core.admin_api._check_auth", return_value=(True, "operator@test")):
        response = TestClient(build_asgi_app()).patch(
            "/api/projects/proj_geo",
            json={
                "geographic_mode": "local_markets",
                "local_market_country_codes": ["FR"],
            },
        )

    assert response.status_code == 409
    assert response.json()["code"] == "geographic_preview_required"
    assert not any(
        sql.startswith("INSERT INTO app.project_preferences")
        for sql, _params in connection.executed
    )

def test_cross_project_patch_is_non_disclosing_and_does_not_mutate(monkeypatch):
    connection = _Connection()
    refusals = []
    monkeypatch.setattr("core.db.get_connection", lambda: connection)
    monkeypatch.setattr(
        "core.project_access.identity_can_access_project_in_org",
        lambda project_id, identity, conn: False,
    )
    monkeypatch.setattr(
        "core.admin_api.write_audit_row",
        lambda **kwargs: refusals.append(kwargs),
    )

    with patch("core.admin_api._check_auth", return_value=(True, "intruder@test")):
        response = TestClient(build_asgi_app()).patch(
            "/api/projects/proj_other",
            json={
                "geographic_mode": "local_markets",
                "local_market_country_codes": ["FR"],
            },
        )

    assert response.status_code == 404
    assert response.json() == {"code": "not_found", "message": "Project not found"}
    assert not any(
        sql.startswith("INSERT INTO app.project_preferences")
        for sql, _params in connection.executed
    )
    assert refusals[0]["action"] == "access_denied"
    assert refusals[0]["metadata"]["operation"] == "patch_project"
