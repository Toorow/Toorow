"""REST and persistence seams for project geographic posture (Story 37.1)."""

from __future__ import annotations

import json

from core import admin_api
from core.country_vocabulary import CountryVocabularyError
from starlette.testclient import TestClient


async def _authorized(_request):
    return True, "operator@example.test"


async def _unauthorized(_request):
    return False, ""


def test_country_vocabulary_route_is_registered_and_authenticated(monkeypatch):
    monkeypatch.setattr(admin_api, "_check_auth", _authorized)

    response = TestClient(admin_api.router).get("/api/vocabularies/countries")

    assert response.status_code == 200
    payload = response.json()
    assert payload["countries"] == sorted(payload["countries"], key=lambda row: row["code"])
    assert {"code": "FR", "display_name": "France"} in payload["countries"]


def test_country_vocabulary_route_rejects_anonymous_callers(monkeypatch):
    monkeypatch.setattr(admin_api, "_check_auth", _unauthorized)

    response = TestClient(admin_api.router).get("/api/vocabularies/countries")

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_country_vocabulary_route_fails_closed(monkeypatch):
    monkeypatch.setattr(admin_api, "_check_auth", _authorized)

    def unavailable():
        raise CountryVocabularyError("private path must not leak")

    monkeypatch.setattr(admin_api, "get_country_vocabulary", unavailable)
    response = TestClient(admin_api.router).get("/api/vocabularies/countries")

    assert response.status_code == 503
    assert response.json() == {
        "code": "country_vocabulary_unavailable",
        "message": "Canonical country vocabulary is unavailable.",
    }


def test_geographic_validation_error_is_field_oriented():
    from core.geographic_reporting import InvalidGeographicPosture

    response = admin_api._geographic_error(
        InvalidGeographicPosture("local_markets requires at least one tracked country")
    )
    payload = json.loads(response.body)

    assert response.status_code == 422
    assert payload["code"] == "invalid_geographic_posture"
    assert "local_market_country_codes" in payload["details"]


class _Cursor:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class _Connection:
    def __init__(self, rows=None):
        self.cursor_instance = _Cursor(rows)

    def cursor(self):
        return self.cursor_instance


def test_missing_preference_row_resolves_to_legacy_global_default():
    conn = _Connection([None])

    posture = admin_api._fetch_geographic_prefs("proj_legacy", conn)

    assert posture.as_dict() == {
        "geographic_mode": "global",
        "local_market_country_codes": [],
    }


def test_geographic_upsert_writes_both_invariant_fields_together():
    from core.geographic_reporting import GeographicPosture

    conn = _Connection()
    posture = GeographicPosture("local_markets", ("DE", "FR"))

    admin_api._upsert_geographic_prefs("proj_one", posture, conn)

    [(sql, params)] = conn.cursor_instance.executed
    assert "geographic_mode = EXCLUDED.geographic_mode" in sql
    assert "local_market_country_codes = EXCLUDED.local_market_country_codes" in sql
    assert params == ("proj_one", "local_markets", ["DE", "FR"])


def test_country_vocabulary_route_is_wired_through_build_asgi_app(monkeypatch):
    monkeypatch.setattr(admin_api, "_check_auth", _authorized)
    monkeypatch.setenv("HEALTH_POLLER_ENABLED", "false")
    monkeypatch.setenv("QUEUE_WORKER_ENABLED", "false")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    from core.main import build_asgi_app

    response = TestClient(build_asgi_app()).get("/api/vocabularies/countries")

    assert response.status_code == 200
    assert response.json()["countries"][0].keys() == {"code", "display_name"}
