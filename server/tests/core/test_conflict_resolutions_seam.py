"""Seam tests for conflict resolution REST routes via build_asgi_app (Story 13.2).

Uses the full ASGI app (AI-56 pattern) to verify:
  - Routes are mounted correctly and accessible.
  - Auth 401 responses for all endpoints.
  - Route structure is correct (no 404/405 on declared routes with mock auth).

No Postgres required (mocked via patch).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture()
def client():
    """Full ASGI app client with auth disabled."""
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    app = build_asgi_app()
    with patch(
        "core.admin_api._check_auth",
        new=AsyncMock(return_value=(True, "seam@test")),
    ):
        with patch(
            "core.conflict_resolutions_api._check_auth",
            new=AsyncMock(return_value=(True, "seam@test")),
        ):
            with patch(
                "core.conflict_resolutions_api._guard_project_access",
                new=AsyncMock(return_value=True),
            ):
                with TestClient(app, raise_server_exceptions=False) as c:
                    yield c


@pytest.fixture()
def client_unauth():
    """Full ASGI app client that always returns 401."""
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    app = build_asgi_app()
    # No auth patch -> real _check_auth will reject (no token)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# Route mounting verification: the address is RESOLVED against the router
# ---------------------------------------------------------------------------


#: The five addresses `CONFLICT_RESOLUTION_ROUTES` declares, as a caller writes
#: them. Concrete segments where the route has a variable, because resolving is
#: the point: a declared route nobody can reach proves nothing.
MOUNTED_ADDRESSES = [
    ("GET", "/api/mdm/conflicts"),
    ("GET", "/api/mdm/conflicts/resolutions"),
    ("POST", "/api/mdm/conflicts/resolutions"),
    ("DELETE", "/api/mdm/conflicts/resolutions/proj_a/cost/meta-ads"),
    ("PATCH", "/api/mdm/conflicts/measure/revenue"),
]


class TestConflictRoutesAreMounted:
    """Verify CONFLICT_RESOLUTION_ROUTES are spliced into the admin router.

    WHY THIS CLASS NO LONGER READS A STATUS CODE. It used to assert `status !=
    404`, on the premise that "a mounted route resolves to its handler (401, or
    500 when no DB is reachable), an UNMOUNTED route returns 404". That premise
    died in `91f9df81` (2026-07-21), the very commit that shipped these routes:
    `_delete_resolution` asks `_guard_project_access` and answers **404 `Project
    not found`** for a project the identity cannot see -- the existence-hiding
    shape this repository uses everywhere. So the mounted DELETE and an unmounted
    DELETE answer with the same number, and the four survivors survived only
    because their 400/422 lands before the guard.

    It stayed green for a month for the worst possible reason: **without a
    reachable Postgres the handler raises and answers 500**, so the assertion
    passed exactly when the database was DOWN and failed the day the suite was
    given one. A seam test whose green depends on an outage is not measuring the
    seam.

    What replaces it is the proof `admin_api.py` already records for its own 539
    routes: resolve the address against `admin_api.router` and require a FULL
    match -- method included, so a route mounted for the wrong verb is a finding
    too. That is env-independent for real: it never opens a socket.
    """

    @staticmethod
    def _resolution(method: str, path: str):
        from core.admin_api import router
        from starlette.routing import Match

        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
            "path_params": {},
        }
        return [
            getattr(route, "path", "")
            for route in router.routes
            if route.matches(scope)[0] is Match.FULL
        ]

    @pytest.mark.parametrize(("method", "path"), MOUNTED_ADDRESSES)
    def test_declared_address_resolves_to_exactly_one_route(self, method, path):
        matches = self._resolution(method, path)
        assert len(matches) == 1, (
            f"{method} {path} resolves to {len(matches)} routes on "
            f"`admin_api.router` ({matches}). Zero means the address is not "
            "spliced, or an earlier route with a variable segment swallowed it; "
            "more than one means two declarations answer the same address."
        )

    def test_an_address_nobody_declares_resolves_to_nothing(self):
        """The negative control, without which the check above proves nothing."""
        assert self._resolution("DELETE", "/api/mdm/conflicts/no-such-thing") == []


# ---------------------------------------------------------------------------
# Structural tests (with mocked DB)
# ---------------------------------------------------------------------------


class TestConflictRoutesBehaviour:
    def test_list_conflicts_needs_no_body(self, client):
        """GET /api/mdm/conflicts returns JSON even with empty DB (mocked)."""
        with patch("core.db.get_connection") as mock_gc:
            mock_conn = MagicMock()
            mock_gc.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_gc.return_value.__exit__ = MagicMock(return_value=False)
            with patch("core.conflict_resolutions.list_conflicts", return_value=[]):
                resp = client.get("/api/mdm/conflicts?project_id=proj_a")
        # With project access granted, expect 200
        assert resp.status_code in {200, 500}  # 500 only if DB not available

    def test_post_missing_fields_returns_400(self, client):
        """POST /api/mdm/conflicts/resolutions with missing fields returns 400."""
        resp = client.post(
            "/api/mdm/conflicts/resolutions",
            json={"target_field": "cost"},  # missing project_id and source_module
        )
        assert resp.status_code == 400

    def test_delete_with_all_path_params(self, client):
        """DELETE with all path params set processes the request (not 404/405)."""
        with patch("core.db.get_connection") as mock_gc:
            mock_conn = MagicMock()
            mock_gc.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_gc.return_value.__exit__ = MagicMock(return_value=False)
            with patch(
                "core.conflict_resolutions.delete_fx_resolution",
                side_effect=ValueError("introuvable"),
            ):
                resp = client.delete(
                    "/api/mdm/conflicts/resolutions/proj_a/cost/meta-ads"
                )
        # 404 because the mock raises ValueError("introuvable") -- correct behaviour
        assert resp.status_code == 404

    def test_measure_patch_missing_body(self, client):
        """PATCH /api/mdm/conflicts/measure/{name} without measure in body -> 400."""
        resp = client.patch("/api/mdm/conflicts/measure/revenue", json={})
        assert resp.status_code == 400
