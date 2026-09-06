"""The /admin console mount serves the SPA for real — assets AND deep links.

Measured 2026-08-07 during the live browser pass: the console build uses an
ABSOLUTE base (its index.html calls /assets/… at the root), but the dispatcher
only served /admin/* — so `/admin/` answered 200 with a blank page (every
bundle 404) and a deep link answered a bare 500 (StaticFiles' 404 propagating
through the dispatcher). A mount that answers 200 and renders nothing is the
worst state: it looks alive.

These probes run against a throwaway dist (ADMIN_DIST_PATH), not the real
build: the contract is the dispatcher's, not the bundle's.
"""

from __future__ import annotations

import os

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from pathlib import Path

import pytest
from starlette.testclient import TestClient

_INDEX = "<!doctype html><html><body>toorow console</body></html>"


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(_INDEX, encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log('boot')", encoding="utf-8")
    monkeypatch.setenv("ADMIN_DIST_PATH", str(dist))
    from core.main import build_asgi_app  # noqa: PLC0415

    return TestClient(build_asgi_app())


def test_admin_root_serves_the_index(client):
    resp = client.get("/admin/")
    assert resp.status_code == 200
    assert "toorow console" in resp.text


def test_admin_deep_link_gets_the_spa_fallback_not_a_500(client):
    resp = client.get("/admin/org/o1/project/p1/context-hub/knowledge-graph")
    assert resp.status_code == 200
    assert "toorow console" in resp.text


def test_absolute_base_assets_are_served_from_the_same_dist(client):
    resp = client.get("/assets/app.js")
    assert resp.status_code == 200
    assert "console.log('boot')" in resp.text


def test_a_missing_asset_is_a_plain_404_not_the_index(client):
    # The fallback is for CLIENT ROUTES under /admin. A missing bundle must
    # never be answered with HTML a browser would try to execute as a module.
    resp = client.get("/assets/gone.js")
    assert resp.status_code == 404


def test_admin_lookalike_prefixes_are_not_captured(client):
    # review-2-4 F-02: /adminX is not /admin.
    resp = client.get("/adminX/anything")
    assert resp.status_code not in (200, 500)


def test_path_traversal_outside_the_dist_is_not_served(client):
    resp = client.get("/assets/../secret.txt")
    # Starlette normalises or 404s; what must never happen is a 200 with
    # content from outside the dist.
    assert resp.status_code in (400, 404)
