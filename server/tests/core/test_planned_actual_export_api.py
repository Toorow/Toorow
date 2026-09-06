"""The HTTP door of the planned-versus-actual extract (story 62.2).

WHAT IS DECIDED HERE AND NOWHERE ELSE. `test_planned_actual_export.py` proves the
gates and the shape; `test_planned_actual_export_pg.py` proves the read. The route
decides the four answers a caller must be able to tell apart -- 401 with no
identity, 404 (never 403) for a Project the caller's organization does not own,
422 carrying the refusal's own code, sentence and gesture, and 200 in two
representations of ONE read -- plus the one thing proper to this door: the CSV
headers keep the UNDER-DELIVERY count and the HOLE count apart, so a saved file
does not lose the distinction the provenance was built to make.

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
BASE = f"/api/projects/{PROJECT}/exports/planned-vs-actual"

_BODY = {
    "plan_id": "plan_EXAMPLE",
    "start": "2026-07-01",
    "end": "2026-07-02",
}

_COLUMNS = [
    "date",
    "plan_line_key",
    "plan_line_label",
    "channel",
    "metric",
    "value",
    "currency",
    "plan_version_id",
    "placement_mapping_fingerprint",
]

_ROW = {
    "date": "2026-07-01",
    "plan_line_key": "line-a",
    "plan_line_label": "Brand awareness",
    "channel": "social",
    "metric": "planned_spend",
    "value": 100,
    "currency": "EUR",
    "plan_version_id": "planv_EXAMPLE",
    "placement_mapping_fingerprint": "d" * 64,
}

_PAYLOAD = {
    "columns": _COLUMNS,
    "rows": [_ROW],
    "provenance": {
        "media_plan_version_id": "planv_EXAMPLE",
        "placement_mapping": {"fingerprint": "d" * 64},
        "currency": {"currency": "EUR"},
        "window": {"start": "2026-07-01", "end": "2026-07-02", "days": 2},
        "date_coverage": {"total_holes": 1, "variance_days": {"count": 3}},
    },
}


def _client():
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    return TestClient(build_asgi_app(), raise_server_exceptions=False)


def _post(query: str = "", *, extract=None, allowed=True, body=None):
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
    # The handler imports `build_extract` per call, so the name to replace is the
    # one in its source module.
    engine = patch(
        "core.planned_actual_export.build_extract",
        **({"side_effect": extract} if callable(extract) else {"return_value": extract}),
    )
    with auth, guard, connection, engine:
        with _client() as client:
            return client.post(f"{BASE}{query}", json=_BODY if body is None else body)


def test_the_route_is_mounted_beside_the_first_reader_of_the_seam():
    from core.admin_api import router

    paths = {getattr(route, "path", "") for route in router.routes}
    assert "/api/projects/{project_id}/exports/planned-vs-actual" in paths
    # One family, two readings -- the sibling is where a reader looks for it.
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
    from core.mmm_export import ExportRefused

    def _refuse(*_args, **_kwargs):
        raise ExportRefused(
            "placement_mapping_orphaned",
            "The placement mapping of this plan is not confirmed.",
            "Open Data > the Datastream Workbench > Placements and re-attach it.",
            orphaned_line_keys=["line-gone"],
        )

    response = _post(extract=_refuse)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "placement_mapping_orphaned"
    assert body["gesture"].startswith("Open Data")
    assert body["orphaned_line_keys"] == ["line-gone"]


def test_an_engine_that_blew_up_is_503_and_says_no_file_was_written():
    def _boom(*_args, **_kwargs):
        raise RuntimeError("the mart went away")

    response = _post(extract=_boom)
    assert response.status_code == 503
    assert response.json()["code"] == "planned_vs_actual_extract_unavailable"
    assert "not an empty extract" in response.json()["message"]


def test_the_default_representation_is_the_companion_with_its_provenance():
    response = _post(extract=_PAYLOAD)
    assert response.status_code == 200
    body = response.json()
    assert body["project_id"] == PROJECT
    assert body["columns"] == _COLUMNS
    assert body["provenance"]["placement_mapping"]["fingerprint"] == "d" * 64


def test_the_csv_keeps_the_under_delivery_count_apart_from_the_hole_count():
    response = _post("?format=csv", extract=_PAYLOAD)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    lines = response.text.strip().splitlines()
    assert lines[0] == ",".join(_COLUMNS)
    assert len(lines) == 2
    assert not any(line.startswith("#") for line in lines)
    assert response.headers["X-Toorow-Export-Companion"]
    assert response.headers["X-Toorow-Export-Rows"] == "1"
    assert response.headers["X-Toorow-Export-Plan-Version"] == "planv_EXAMPLE"
    assert response.headers["X-Toorow-Export-Mapping-Fingerprint"] == "d" * 64
    assert response.headers["X-Toorow-Export-Currency"] == "EUR"
    # THE TWO NUMBERS ARE NOT THE SAME NUMBER, and the header proves it.
    assert response.headers["X-Toorow-Export-Date-Gaps"] == "1"
    assert response.headers["X-Toorow-Export-Variance-Days"] == "3"
    assert "planned_vs_actual_2026-07-01_2026-07-02.csv" in (
        response.headers["content-disposition"]
    )


def test_a_body_that_is_not_an_object_reaches_the_engine_as_an_empty_request():
    """No crash, and no guessed defaults: the engine's own refusals answer."""
    seen = {}

    def _capture(_conn, **kwargs):
        seen.update(kwargs)
        return _PAYLOAD

    _post(extract=_capture, body=["plan_EXAMPLE"])
    assert seen["plan_id"] == "" and seen["start"] is None
