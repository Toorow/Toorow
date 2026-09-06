"""REST and persistence seams for project geographic posture (Story 37.1)."""

from __future__ import annotations

import json
import pathlib

import core.projects_api as projects_api  # AD-43 : le handler vit chez son sujet
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

    # AD-43 : la lecture vit chez `reference_vocabulary_api`, qui importe le
    # catalogue A L'APPEL -- donc c'est la SOURCE qu'il faut remplacer ici, pas
    # une liaison d'`admin_api` que plus personne ne lit.
    monkeypatch.setattr(
        "core.country_vocabulary.get_country_vocabulary", unavailable
    )
    response = TestClient(admin_api.router).get("/api/vocabularies/countries")

    assert response.status_code == 503
    assert response.json() == {
        "code": "country_vocabulary_unavailable",
        "message": "Canonical country vocabulary is unavailable.",
    }


def test_the_retired_posture_is_refused_and_names_the_governed_gesture():
    """The door is closed, and the refusal says where the gesture lives now.

    It replaces `test_geographic_validation_error_is_field_oriented`, which
    checked that a rejected posture pointed at the right FIELD -- a useful
    assertion while the field was writable and a meaningless one now that no
    posture is accepted here at all.
    """
    response = projects_api._geographic_posture_retired_response()
    payload = json.loads(response.body)

    assert response.status_code == 422
    assert payload["code"] == "geographic_posture_retired"
    # A refusal that only says "no" sends the caller looking. These are the two
    # surfaces that actually move the governed country model.
    assert "Country registry" in payload["message"]
    assert "publish_country_master_data" in payload["message"]
    assert payload["details"]["retired_fields"] == [
        "geographic_mode",
        "local_market_country_codes",
        "local_markets",
    ]


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

    posture = projects_api._fetch_geographic_prefs("proj_legacy", conn)

    assert posture.as_dict() == {
        "geographic_mode": "global",
        "local_markets": [],
        "local_market_country_codes": [],
    }


def test_projects_api_no_longer_writes_the_retired_posture_columns():
    """The ratchet, textual on purpose.

    `test_geographic_upsert_writes_both_invariant_fields_together` used to prove
    that `_upsert_geographic_prefs` wrote the three columns together. The helper
    is gone with the door: `country_registry.py` declares those columns replaced
    outright and `country_activation.governed_posture` never reads them, so the
    write moved nothing while emitting a real audit row.

    A behavioural assertion cannot fail on a call site that merely comes back, so
    this reads the module: the persist function may not be imported or called
    from here again without this test being deliberately edited.
    """
    source = (
        pathlib.Path(projects_api.__file__).read_text(encoding="utf-8")
    )

    assert "persist_project_geographic_posture" not in source, (
        "projects_api writes the retired geographic posture again. Countries and "
        "markets are published through the governed Country registry "
        "(governance_surface_api / master_data_mcp); a write here changes no "
        "governed answer."
    )
    assert "_geographic_posture_retired_response" in source


def test_country_vocabulary_route_is_wired_through_build_asgi_app(monkeypatch):
    monkeypatch.setattr(admin_api, "_check_auth", _authorized)
    monkeypatch.setenv("HEALTH_POLLER_ENABLED", "false")
    monkeypatch.setenv("QUEUE_WORKER_ENABLED", "false")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    from core.main import build_asgi_app

    response = TestClient(build_asgi_app()).get("/api/vocabularies/countries")

    assert response.status_code == 200
    assert response.json()["countries"][0].keys() == {"code", "display_name"}
