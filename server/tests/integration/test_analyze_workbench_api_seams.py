"""Story 50.2 -- real-ASGI seams for the Explore facets and the Result lenses.

These run through `build_asgi_app()`, so they prove the routes are actually
MOUNTED. That distinction is not academic in this repository: an orphaned handler
(`_create_datastream_mapping_version`) had a whole test file asserting behaviour
for a route nobody had mounted, and every one of those tests answered 405 for
months without anyone noticing.

Kept small on purpose -- the ASGI seam suite is slow (30-45s per test), so each
test here buys a distinct property rather than a variation of the same one.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.testclient import TestClient

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

BASE = "/api/projects/proj_EXAMPLE/analyze"


def _client() -> TestClient:
    from core.main import build_asgi_app

    return TestClient(build_asgi_app(), raise_server_exceptions=False)


class _ScriptedCursor:
    """Answers by which table the statement names, not by call order."""

    def __init__(self):
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        self._last = sql

    def fetchone(self):
        if "app.projects" in self._last:
            return ("org_EXAMPLE",)
        # Every analytical object read answers "no row": the seam tests assert
        # routing and non-disclosure, not composition. Composition is proved in
        # server/tests/core/test_analyze_workbench.py against real column tuples.
        return None

    def fetchall(self):
        return []


def _connection():
    conn = MagicMock()
    conn.cursor = MagicMock(side_effect=lambda: _ScriptedCursor())
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=conn)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def _authed(role_ok=True):
    from starlette.responses import JSONResponse

    denial = None if role_ok else JSONResponse({"code": "not_found"}, 404)
    return (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person-1"))),
        patch("core.admin_api._require_datastream_role", return_value=denial),
        patch("core.db.get_connection", return_value=_connection()),
    )


#: One concrete address per declared route. The mount proof needs real requests
#: rather than a route-table walk: `core.routing.build_asgi_app` dispatches
#: through its own callable, so the Starlette route objects are not enumerable
#: from the returned app and a table walk would report "not mounted" forever.
_DECLARED_ADDRESSES = (
    f"{BASE}/query-facets",
    f"{BASE}/query-spec-versions/qsv_EXAMPLE",
    f"{BASE}/results/qr_EXAMPLE/lens/view",
)


def test_every_declared_route_is_mounted_in_the_real_application():
    """An UNMOUNTED address answers `text/plain` "Not Found"; a mounted one
    answers this module's JSON envelope. That difference is the mount proof, and
    it is measured rather than asserted from the route list -- an orphaned
    handler is exactly the failure this file exists to catch."""
    auth, role, db = _authed()
    with auth, role, db:
        client = _client()
        unmounted = [
            address
            for address in _DECLARED_ADDRESSES
            if "application/json" not in (client.get(address).headers.get("content-type") or "")
        ]
    assert not unmounted, (
        "these Story 50.2 routes are not mounted in build_asgi_app(); apply the mount "
        f"lines from the story's Dev Agent Record: {unmounted}"
    )


def test_the_lens_route_is_not_swallowed_by_the_result_evidence_route():
    """Literal-before-parameter ordering, asserted at the app level.

    `/results/{id}/evidence` (Story 50.1) and `/results/{id}/lens/{lens}` share a
    prefix. If the first were declared as `/results/{id}/{anything}` the second
    would never be reached, and the workbench would silently show evidence under
    every lens address.
    """
    auth, role, db = _authed()
    with auth, role, db:
        response = _client().get(f"{BASE}/results/qr_EXAMPLE/lens/quality")
    # 404 from the handler (no such Result under this fixture) -- but it is OUR
    # handler's envelope, which proves the route matched.
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_an_undeclared_lens_stays_unknown_and_is_never_repaired_into_view():
    auth, role, db = _authed()
    with auth, role, db:
        response = _client().get(f"{BASE}/results/qr_EXAMPLE/lens/summary")
    assert response.status_code == 404
    assert response.json()["code"] == "unknown_lens"


def test_a_half_pin_is_refused_rather_than_completed_by_the_server():
    auth, role, db = _authed()
    with auth, role, db:
        response = _client().get(f"{BASE}/query-facets?semantic_view_id=sv_EXAMPLE")
    assert response.status_code == 400
    assert response.json()["code"] == "missing_field"


def test_an_unauthenticated_caller_gets_401_before_any_read():
    with patch("core.admin_api._check_auth", new=AsyncMock(return_value=(False, None))):
        response = _client().get(f"{BASE}/results/qr_EXAMPLE/lens/view")
    assert response.status_code == 401


def test_a_denied_project_is_indistinguishable_from_an_absent_one():
    auth, role, db = _authed(role_ok=False)
    with auth, role, db:
        denied = _client().get(f"{BASE}/results/qr_EXAMPLE/lens/view")
    auth, role, db = _authed()
    with auth, role, db:
        absent = _client().get(f"{BASE}/results/qr_ABSENT/lens/view")
    assert denied.status_code == absent.status_code == 404
    assert denied.json()["code"] == absent.json()["code"] == "not_found"
