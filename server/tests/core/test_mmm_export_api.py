"""The HTTP door of the MMM extract (story 62.1).

WHAT IS DECIDED HERE AND NOWHERE ELSE. `test_mmm_export.py` proves the gates and
the shape of the file; `test_mmm_export_pg.py` proves the read. The route decides
four answers a caller must be able to tell apart, and no other test reaches them:

  * **401** with no identity -- no answer, not an empty file;
  * **404** for a Project the caller's organization does not own, and never 403:
    a 403 would confirm that the id exists;
  * **422** carrying the refusal's own `code`, `message` and `gesture`, so a
    screen can render the sentence beside the field that caused it;
  * **200** in two representations of ONE read -- the JSON companion, and the
    plain CSV whose headers name the companion rather than implying it.

No database: what is proven is the branching.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

IDENTITY = "owner@example.com"
PROJECT = "proj_EXAMPLE"
BASE = f"/api/projects/{PROJECT}/exports/mmm"

_BODY = {
    "semantic_view_version_id": "svv_EXAMPLE",
    "metrics": ["spend"],
    "dimensions": ["channel"],
    "start": "2026-07-01",
    "end": "2026-07-02",
}

_PAYLOAD = {
    "columns": ["date", "channel", "metric", "value", "currency",
                "measurement_grain_version_id"],
    "rows": [
        {"date": "2026-07-01", "channel": "search", "metric": "spend",
         "value": 10.5, "currency": "EUR", "measurement_grain_version_id": "mgv_1"},
    ],
    "provenance": {
        "semantic_view_version_id": "svv_EXAMPLE",
        "request_hash": "c" * 64,
        "window": {"start": "2026-07-01", "end": "2026-07-02", "days": 2},
        "date_gaps": {"total": 1},
    },
}


def _client():
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    return TestClient(build_asgi_app(), raise_server_exceptions=False)


def _post(query: str = "", *, extract=None, allowed=True, body=None):
    """Post with the guard resolved and `build_extract` standing in for the engine."""

    class _Decision:
        allowed = True
        org_id = "org_EXAMPLE"

    class _Denied:
        allowed = False
        org_id = None

    auth = patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, IDENTITY)))
    guard = patch(
        "core.project_access.resolve_strict_resource_access",
        return_value=_Decision() if allowed else _Denied(),
    )
    connection = patch("core.query_specs_api.analyze_connection")
    # `mmm_export_api` imports `build_extract` INSIDE the handler, so the name to
    # replace is the one in its source module -- the route binds it per call.
    engine = patch(
        "core.mmm_export.build_extract",
        **({"side_effect": extract} if callable(extract) else {"return_value": extract}),
    )
    with auth, guard, connection, engine:
        with _client() as client:
            return client.post(f"{BASE}{query}", json=_BODY if body is None else body)


def test_the_route_is_mounted_at_the_address_the_extract_is_asked_for():
    from core.admin_api import router

    paths = {getattr(route, "path", "") for route in router.routes}
    assert "/api/projects/{project_id}/exports/mmm" in paths


def test_no_authentication_is_no_file():
    with patch("core.admin_api._check_auth", new=AsyncMock(return_value=(False, ""))):
        with _client() as client:
            response = client.post(BASE, json=_BODY)
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_a_project_the_caller_does_not_own_answers_404_and_never_403():
    response = _post(allowed=False, extract=_PAYLOAD)
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_a_refusal_travels_whole_with_its_code_its_sentence_and_its_gesture():
    from core.mmm_export import MmmExportRefused

    def _refuse(*_args, **_kwargs):
        raise MmmExportRefused(
            "breakdown_dimension_not_in_grain",
            "No live measurement grain relates channel to spend.",
            "Append a version to the measurement grain of this metric.",
            metric="spend",
        )

    response = _post(extract=_refuse)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "breakdown_dimension_not_in_grain"
    assert body["gesture"].startswith("Append a version")
    assert body["metric"] == "spend"


def test_an_engine_that_blew_up_is_503_and_says_no_file_was_written():
    def _boom(*_args, **_kwargs):
        raise RuntimeError("the store went away")

    response = _post(extract=_boom)
    assert response.status_code == 503
    assert response.json()["code"] == "mmm_extract_unavailable"
    assert "not an empty extract" in response.json()["message"]


def test_the_default_representation_is_the_companion_with_its_provenance():
    response = _post(extract=_PAYLOAD)
    assert response.status_code == 200
    body = response.json()
    assert body["project_id"] == PROJECT
    assert body["columns"] == _PAYLOAD["columns"]
    assert body["provenance"]["request_hash"] == "c" * 64


def test_the_csv_representation_is_the_table_alone_and_names_its_companion():
    response = _post("?format=csv", extract=_PAYLOAD)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    lines = response.text.strip().splitlines()
    assert lines[0] == ",".join(_PAYLOAD["columns"])
    assert len(lines) == 2
    assert not any(line.startswith("#") for line in lines)
    # The provenance a saved file would otherwise lose: named, with the request
    # that produces it, and never implied.
    assert response.headers["X-Toorow-Export-Companion"]
    assert response.headers["X-Toorow-Export-View-Version"] == "svv_EXAMPLE"
    assert response.headers["X-Toorow-Export-Rows"] == "1"
    assert response.headers["X-Toorow-Export-Date-Gaps"] == "1"
    assert "2026-07-01_2026-07-02.csv" in response.headers["content-disposition"]


def test_a_metric_sent_as_a_bare_string_is_not_iterated_character_by_character():
    seen = {}

    def _capture(_conn, **kwargs):
        seen.update(kwargs)
        return _PAYLOAD

    _post(extract=_capture, body={**_BODY, "metrics": "spend"})
    assert seen["metrics"] == ["spend"]


def test_a_body_that_is_not_an_object_reaches_the_engine_as_an_empty_request():
    """No crash, and no guessed defaults: the engine's own refusals answer."""
    seen = {}

    def _capture(_conn, **kwargs):
        seen.update(kwargs)
        return _PAYLOAD

    _post(extract=_capture, body=["spend"])
    assert seen["metrics"] == [] and seen["semantic_view_version_id"] == ""
