"""AI-56 seam tests for POST /api/timezone/day-offset-check via build_asgi_app (Story 39.8).

Uses the full ASGI app (AI-56 pattern, mirrors test_money_api_seam.py) to verify:
  - Route is mounted (401 without token, not 404/405) [22].
  - Two datastreams, different captured tz -> 200 {signalled:true, signal:{...
    distinct_timezones, realignable:false}} [23].
  - Two datastreams sharing one tz -> 200 {signalled:false} (AC3) [24].
  - One undetermined-tz datastream + one known -> 200 {signalled:false} (defer to 39.7) [25].
  - A source declaring report_timezone_lever -> stream carries has_lever:true + lever_hint;
    a GAM-like datastream (no lever) -> has_lever:false ("fixed, no lever") [26].
  - Cross-project project_id -> 404 (non-disclosant) [27].
  - Malformed body (no datastreams) -> 422 [28].

Postgres is mocked (the project guard) and the per-stream resolvers are patched so no DB /
manifest registry is required.
"""

from __future__ import annotations

import os

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

_ROUTE = "/api/timezone/day-offset-check"


@pytest.fixture()
def make_client():
    """Factory building a patched TestClient (auth+guard+resolvers) as a context manager."""
    from contextlib import contextmanager

    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    @contextmanager
    def _factory(*, guard=True, tz_map=None, lever_map=None, patch_auth=True):
        tz_map = tz_map or {}
        lever_map = lever_map or {}

        def fake_tz(ds):
            return tz_map.get(str(ds))

        def fake_lever(ds):
            return lever_map.get(str(ds), (False, None))

        app = build_asgi_app()
        patches = []
        if patch_auth:
            patches += [
                patch(
                    "core.timezone_api._check_auth",
                    new=AsyncMock(return_value=(True, "seam@test")),
                ),
                patch(
                    "core.timezone_api._guard_project_access",
                    new=AsyncMock(return_value=guard),
                ),
                patch("core.timezone_api._resolve_report_timezone", side_effect=fake_tz),
                patch("core.timezone_api._resolve_lever", side_effect=fake_lever),
            ]
        gc_patch = patch("core.db.get_connection")
        with (
            _apply(patches),
            gc_patch as mock_gc,
        ):
            mock_conn = MagicMock()
            mock_gc.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_gc.return_value.__exit__ = MagicMock(return_value=False)
            with TestClient(app, raise_server_exceptions=False) as client:
                yield client

    return _factory


class _apply:
    """Enter/exit a list of patch objects as one context manager."""

    def __init__(self, patches):
        self._patches = patches

    def __enter__(self):
        self._started = [p.start() for p in self._patches]
        return self._started

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


# ---------------------------------------------------------------------------
# 22 -- route mounted
# ---------------------------------------------------------------------------


def test_route_mounted_without_token():  # 22
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    app = build_asgi_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.post(_ROUTE, json={"datastreams": []})
    assert resp.status_code != 404, "route not spliced into admin_api"
    assert resp.status_code in (401, 500)


# ---------------------------------------------------------------------------
# 23 -- different tz -> signalled:true
# ---------------------------------------------------------------------------


def test_different_tz_signalled_true(make_client):  # 23
    with make_client(
        tz_map={"ds1": "Europe/Paris", "ds2": "UTC"},
    ) as client:
        resp = client.post(
            _ROUTE,
            json={"project_id": "proj_a", "metric": "revenue",
                  "datastreams": ["ds1", "ds2"]},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["signalled"] is True
    signal = data["signal"]
    assert signal["distinct_timezones"] == ["Europe/Paris", "UTC"]
    assert signal["realignable"] is False
    assert signal["severity"] == "advisory"


# ---------------------------------------------------------------------------
# 24 -- same tz -> signalled:false
# ---------------------------------------------------------------------------


def test_same_tz_signalled_false(make_client):  # 24
    with make_client(
        tz_map={"ds1": "Europe/Paris", "ds2": "Europe/Paris"},
    ) as client:
        resp = client.post(
            _ROUTE,
            json={"project_id": "proj_a", "datastreams": ["ds1", "ds2"]},
        )
    assert resp.status_code == 200
    assert resp.json() == {"signalled": False}


# ---------------------------------------------------------------------------
# 25 -- undetermined + known -> signalled:false (defer to 39.7)
# ---------------------------------------------------------------------------


def test_undetermined_plus_known_signalled_false(make_client):  # 25
    with make_client(
        tz_map={"ds1": None, "ds2": "UTC"},
    ) as client:
        resp = client.post(
            _ROUTE,
            json={"project_id": "proj_a", "datastreams": ["ds1", "ds2"]},
        )
    assert resp.status_code == 200
    assert resp.json() == {"signalled": False}


# ---------------------------------------------------------------------------
# 26 -- lever posture (declared vs GAM-like no lever)
# ---------------------------------------------------------------------------


def test_lever_posture_surfaced(make_client):  # 26
    with make_client(
        tz_map={"ds_lever": "Europe/Paris", "ds_gam": "UTC"},
        lever_map={"ds_lever": (True, "report_settings"), "ds_gam": (False, None)},
    ) as client:
        resp = client.post(
            _ROUTE,
            json={"project_id": "proj_a", "datastreams": ["ds_lever", "ds_gam"]},
        )
    assert resp.status_code == 200
    signal = resp.json()["signal"]
    by_ds = {s["datastream"]: s for s in signal["report_timezones"]}
    assert by_ds["ds_lever"]["has_lever"] is True
    assert by_ds["ds_lever"]["lever_hint"] == "report_settings"
    assert by_ds["ds_gam"]["has_lever"] is False  # GAM: fixed, no lever
    assert by_ds["ds_gam"]["lever_hint"] is None


# ---------------------------------------------------------------------------
# 27 -- cross-project -> 404
# ---------------------------------------------------------------------------


def test_cross_project_returns_404(make_client):  # 27
    with make_client(
        guard=False,
        tz_map={"ds1": "Europe/Paris", "ds2": "UTC"},
    ) as client:
        resp = client.post(
            _ROUTE,
            json={"project_id": "proj_other", "datastreams": ["ds1", "ds2"]},
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 28 -- malformed body -> 422
# ---------------------------------------------------------------------------


def test_missing_datastreams_returns_422(make_client):  # 28
    with make_client() as client:
        resp = client.post(_ROUTE, json={"project_id": "proj_a"})
    assert resp.status_code == 422


def test_datastreams_not_a_list_returns_422(make_client):
    with make_client() as client:
        resp = client.post(_ROUTE, json={"project_id": "proj_a", "datastreams": "nope"})
    assert resp.status_code == 422
