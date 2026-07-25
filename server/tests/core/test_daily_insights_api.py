"""Seam tests for the daily-insights inbox REST API (Epic 35, Story 35.4, AI-56).

Exercises ``core.daily_insights_api`` at the ``build_asgi_app()`` level (routes spliced
into the admin router) with a Starlette ``TestClient``. All DB access is mocked -- no real
Postgres. The store / journal / recipe / share layers are stubbed via monkeypatch: this
suite proves the wiring (auth, AD-5 non-disclosing 404, share-lineage 409, recipe), not
the reused building blocks (which own their own tests).

Coverage:
  - GET  /runs                       -> 200 {runs, total} shape (journal-formatted)
  - GET  /runs/{date}                -> 404 when the run is absent
  - AD-5 denial (identity_has_project_access -> False) -> 404 non-disclosing
  - POST /insights/{id}/share        -> 409 when the insight has no render_snapshot_id
  - POST /insights/{id}/share        -> 201 success path (create_share mocked)
  - GET  /recipe                     -> 200 {recipe, text} + 400 on a bad hour

Strategy: HEALTH_POLLER_ENABLED / QUEUE_WORKER_ENABLED / SCHEDULER_ENABLED = "false".
Auth passes by stubbing ``core.api_auth.authenticate_api_request``.

ASCII-only stdout (L-3).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_asgi_app():
    from core.main import build_asgi_app as _build

    return _build()


def _fake_get_connection():
    """Return a context-manager factory yielding a mocked psycopg connection."""
    conn = MagicMock()

    @contextmanager
    def _factory():
        yield conn

    return _factory


async def _auth_ok(request):
    return True, "test_user"


def _client(app):
    from starlette.testclient import TestClient

    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# GET /api/daily-insights/runs
# ---------------------------------------------------------------------------


def test_list_runs_ok_shape():
    """GET /runs -> 200 with {runs: [...], total: N}; each run is journal-formatted."""
    runs = [
        {
            "id": "dir_1",
            "project_id": "proj_a",
            "insight_date": "2026-07-21",
            "status": "published",
            "period_from": "2026-07-20",
            "period_to": "2026-07-20",
            "host": "acme-llm",
            "prompt_version": "v1",
            "contract_version": "1",
            "created_at": "2026-07-21T07:00:00+00:00",
            "updated_at": "2026-07-21T07:00:00+00:00",
        }
    ]

    with (
        patch("core.api_auth.authenticate_api_request", new=_auth_ok),
        patch("core.db.get_connection", new=_fake_get_connection()),
        patch("core.project_access.identity_has_project_access", return_value=True),
        patch("core.daily_insights.list_runs", return_value=runs),
    ):
        app = build_asgi_app()
        resp = _client(app).get("/api/daily-insights/runs?project_id=proj_a")

    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["total"] == 1
    assert len(body["runs"]) == 1
    journal = body["runs"][0]
    # run_journal shape (35.5): distinct state + provenance + insightDate.
    assert journal["state"] == "published"
    assert journal["insightDate"] == "2026-07-21"
    assert journal["provenance"]["host"] == "acme-llm"


def test_list_runs_ad5_denied():
    """GET /runs with no project access -> 404 non-disclosing (AD-5)."""
    with (
        patch("core.api_auth.authenticate_api_request", new=_auth_ok),
        patch("core.db.get_connection", new=_fake_get_connection()),
        patch("core.project_access.identity_has_project_access", return_value=False),
    ):
        app = build_asgi_app()
        resp = _client(app).get("/api/daily-insights/runs?project_id=proj_other")

    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_list_runs_missing_project_id():
    """GET /runs without project_id -> 422."""
    with (
        patch("core.api_auth.authenticate_api_request", new=_auth_ok),
        patch("core.db.get_connection", new=_fake_get_connection()),
    ):
        app = build_asgi_app()
        resp = _client(app).get("/api/daily-insights/runs")

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/daily-insights/runs/{insight_date}
# ---------------------------------------------------------------------------


def test_get_run_404_when_absent():
    """GET /runs/{date} -> 404 when get_run returns None (task did not run)."""
    with (
        patch("core.api_auth.authenticate_api_request", new=_auth_ok),
        patch("core.db.get_connection", new=_fake_get_connection()),
        patch("core.project_access.identity_has_project_access", return_value=True),
        patch("core.daily_insights.get_run", return_value=None),
    ):
        app = build_asgi_app()
        resp = _client(app).get("/api/daily-insights/runs/2026-07-21?project_id=proj_a")

    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_get_run_ok_shape():
    """GET /runs/{date} -> 200 with {run: journal, insights: [...]}."""
    run = {
        "id": "dir_1",
        "project_id": "proj_a",
        "insight_date": "2026-07-21",
        "status": "published",
        "period_from": "2026-07-20",
        "period_to": "2026-07-20",
        "coverage": {},
        "host": "acme-llm",
        "prompt_version": "v1",
        "contract_version": "1",
        "identity": "user_1",
        "trace_id": "trace_1",
        "created_at": "2026-07-21T07:00:00+00:00",
        "updated_at": "2026-07-21T07:00:00+00:00",
        "insights": [{"id": "din_1", "slot": 0, "payload": {"schemaVersion": "1"}}],
    }

    with (
        patch("core.api_auth.authenticate_api_request", new=_auth_ok),
        patch("core.db.get_connection", new=_fake_get_connection()),
        patch("core.project_access.identity_has_project_access", return_value=True),
        patch("core.daily_insights.get_run", return_value=run),
    ):
        app = build_asgi_app()
        resp = _client(app).get("/api/daily-insights/runs/2026-07-21?project_id=proj_a")

    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["run"]["state"] == "published"
    assert body["run"]["itemCount"] == 1
    assert len(body["insights"]) == 1
    assert body["insights"][0]["id"] == "din_1"


# ---------------------------------------------------------------------------
# POST /api/daily-insights/insights/{insight_id}/share
# ---------------------------------------------------------------------------


def test_share_409_when_no_snapshot_lineage():
    """POST /insights/{id}/share -> 409 when the insight has no render_snapshot_id."""
    insight = {"id": "din_1", "project_id": "proj_a", "render_snapshot_id": None}

    with (
        patch("core.api_auth.authenticate_api_request", new=_auth_ok),
        patch("core.db.get_connection", new=_fake_get_connection()),
        patch("core.project_access.identity_has_project_access", return_value=True),
        patch("core.daily_insights.get_insight", return_value=insight),
    ):
        app = build_asgi_app()
        resp = _client(app).post(
            "/api/daily-insights/insights/din_1/share?project_id=proj_a"
        )

    assert resp.status_code == 409
    assert resp.json()["code"] == "no_snapshot_lineage"


def test_share_success_path():
    """POST /insights/{id}/share -> 201 {share_id, token, url} via create_share."""
    insight = {"id": "din_1", "project_id": "proj_a", "render_snapshot_id": "rsn_1"}

    with (
        patch("core.api_auth.authenticate_api_request", new=_auth_ok),
        patch("core.db.get_connection", new=_fake_get_connection()),
        patch("core.project_access.identity_has_project_access", return_value=True),
        patch("core.daily_insights.get_insight", return_value=insight),
        patch("core.snapshot_shares.create_share", return_value=("rss_1", "tok_abc")),
    ):
        app = build_asgi_app()
        resp = _client(app).post(
            "/api/daily-insights/insights/din_1/share?project_id=proj_a"
        )

    assert resp.status_code == 201, f"got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["share_id"] == "rss_1"
    assert body["token"] == "tok_abc"
    assert body["url"] == "/api/rendus/shared/tok_abc"


def test_share_404_when_insight_absent():
    """POST /insights/{id}/share -> 404 when the insight does not resolve."""
    with (
        patch("core.api_auth.authenticate_api_request", new=_auth_ok),
        patch("core.db.get_connection", new=_fake_get_connection()),
        patch("core.project_access.identity_has_project_access", return_value=True),
        patch("core.daily_insights.get_insight", return_value=None),
    ):
        app = build_asgi_app()
        resp = _client(app).post(
            "/api/daily-insights/insights/din_missing/share?project_id=proj_a"
        )

    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


# ---------------------------------------------------------------------------
# GET /api/daily-insights/recipe
# ---------------------------------------------------------------------------


def test_recipe_ok_shape():
    """GET /recipe -> 200 with {recipe, text} (35.5 exposure, frozen open)."""
    with (
        patch("core.api_auth.authenticate_api_request", new=_auth_ok),
        patch("core.db.get_connection", new=_fake_get_connection()),
        patch("core.project_access.identity_has_project_access", return_value=True),
    ):
        app = build_asgi_app()
        resp = _client(app).get(
            "/api/daily-insights/recipe?project_id=proj_a&timezone=Europe/Paris&hour=7"
        )

    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["recipe"]["project"] == "proj_a"
    assert body["recipe"]["schedule"]["hourLocal"] == 7
    assert body["recipe"]["schedule"]["timezone"] == "Europe/Paris"
    assert body["recipe"]["contractVersion"] == "1"
    assert isinstance(body["text"], str) and "daily-insight task" in body["text"]


def test_recipe_default_timezone_and_hour():
    """GET /recipe without timezone/hour -> defaults Europe/Paris @ 07:00."""
    with (
        patch("core.api_auth.authenticate_api_request", new=_auth_ok),
        patch("core.db.get_connection", new=_fake_get_connection()),
        patch("core.project_access.identity_has_project_access", return_value=True),
    ):
        app = build_asgi_app()
        resp = _client(app).get("/api/daily-insights/recipe?project_id=proj_a")

    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["recipe"]["schedule"]["timezone"] == "Europe/Paris"
    assert body["recipe"]["schedule"]["hourLocal"] == 7


def test_recipe_bad_hour_400():
    """GET /recipe with an out-of-range hour -> 400."""
    with (
        patch("core.api_auth.authenticate_api_request", new=_auth_ok),
        patch("core.db.get_connection", new=_fake_get_connection()),
        patch("core.project_access.identity_has_project_access", return_value=True),
    ):
        app = build_asgi_app()
        resp = _client(app).get("/api/daily-insights/recipe?project_id=proj_a&hour=99")

    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_params"


def test_recipe_non_integer_hour_400():
    """GET /recipe with a non-integer hour -> 400."""
    with (
        patch("core.api_auth.authenticate_api_request", new=_auth_ok),
        patch("core.db.get_connection", new=_fake_get_connection()),
        patch("core.project_access.identity_has_project_access", return_value=True),
    ):
        app = build_asgi_app()
        resp = _client(app).get("/api/daily-insights/recipe?project_id=proj_a&hour=noon")

    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_params"
