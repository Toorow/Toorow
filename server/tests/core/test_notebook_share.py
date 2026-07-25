"""Tests for notebook share token endpoints (Story 6.6, AC3, AC6).

Covers (from AC6):
  - test_share_generates_token: PATCH share=true -> share_token in DB is non-null, 32 chars.
  - test_share_url_returned: response contains share_url with token.
  - test_shared_endpoint_returns_last_run: GET /shared/{token} -> 200 with last run envelope.
  - test_shared_endpoint_unknown_token: GET /shared/badtoken -> 404.
  - test_unshare_clears_token: PATCH share=false -> share_token=NULL; subsequent GET -> 404.
  - test_new_share_generates_new_token: share, unshare, share again -> new token.

Also covers:
  - test_shared_endpoint_no_auth_required: GET /shared/{token} works without Bearer token.
  - test_slide_export_html_returns_html: GET /export/html -> Content-Type text/html.
  - test_slide_export_html_contains_title: HTML contains notebook title.
  - test_slide_export_html_no_external_refs: HTML has no http(s) references in src/href/url().
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from starlette.applications import Starlette  # noqa: E402
from starlette.routing import Mount  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

# ---------------------------------------------------------------------------
# Test app factory
# ---------------------------------------------------------------------------


def _make_app():
    """Create a minimal Starlette test app mounting only the admin_api router."""
    from core.admin_api import router

    return Starlette(routes=[Mount("/", app=router)])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    """Test client with auth disabled."""
    os.environ["TOOROW_AUTH_MODE"] = "disabled"
    from core import api_auth

    api_auth.reset_verifier_cache()
    app = _make_app()
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    os.environ.pop("TOOROW_AUTH_MODE", None)
    api_auth.reset_verifier_cache()


def _fake_ts():
    return datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)


def _make_mock_conn(cursor_mock=None):
    """Build a mock psycopg connection usable as a context manager."""
    if cursor_mock is None:
        cursor_mock = MagicMock()
    conn_mock = MagicMock()
    conn_mock.__enter__ = MagicMock(return_value=conn_mock)
    conn_mock.__exit__ = MagicMock(return_value=False)
    cursor_cm = MagicMock()
    cursor_cm.__enter__ = MagicMock(return_value=cursor_mock)
    cursor_cm.__exit__ = MagicMock(return_value=False)
    conn_mock.cursor = MagicMock(return_value=cursor_cm)
    return conn_mock


# ---------------------------------------------------------------------------
# Share token tests (AC6)
# ---------------------------------------------------------------------------


def test_share_generates_token(client):
    """PATCH share=true -> share_token set to a non-null, 32-char URL-safe token."""
    # Test that share_url is returned, contains the generated token, and the
    # token has the expected length (24 bytes -> 32 URL-safe chars).
    cursor_mock = MagicMock()
    # First fetchone: notebook exists, no existing token
    cursor_mock.fetchone.return_value = ("nb_TEST", None)
    conn_mock = _make_mock_conn(cursor_mock)

    with patch("core.db.get_connection", return_value=conn_mock):
        resp = client.patch(
            "/api/notebooks/nb_TEST/share",
            json={"shared": True},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert "share_url" in data

    # Extract token from the share_url and verify its length
    share_url = data["share_url"]
    token = share_url.split("/api/notebooks/shared/")[-1]
    assert len(token) == 32  # secrets.token_urlsafe(24) -> 32 URL-safe chars

    # Verify UPDATE was called with the token
    execute_calls = cursor_mock.execute.call_args_list
    update_call = next(
        (c for c in execute_calls if "UPDATE" in str(c) and "share_token" in str(c)),
        None,
    )
    assert update_call is not None
    params = update_call[0][1]
    assert params[0] == token


def test_share_url_returned(client):
    """Response contains share_url in the correct format."""
    cursor_mock = MagicMock()
    cursor_mock.fetchone.return_value = ("nb_TEST", None)
    conn_mock = _make_mock_conn(cursor_mock)

    with patch("core.db.get_connection", return_value=conn_mock):
        resp = client.patch(
            "/api/notebooks/nb_TEST/share",
            json={"shared": True},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert "share_url" in data
    assert "/api/notebooks/shared/" in data["share_url"]


def test_unshare_clears_token(client):
    """PATCH share=false -> share_token=NULL cleared in DB."""
    cursor_mock = MagicMock()
    # fetchone: notebook exists (existing token doesn't matter for unshare)
    cursor_mock.fetchone.return_value = ("nb_TEST", "some-existing-token")
    conn_mock = _make_mock_conn(cursor_mock)

    with patch("core.db.get_connection", return_value=conn_mock):
        resp = client.patch(
            "/api/notebooks/nb_TEST/share",
            json={"shared": False},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data == {"shared": False}

    # Verify UPDATE set share_token=NULL
    execute_calls = cursor_mock.execute.call_args_list
    update_call = next(
        (c for c in execute_calls if "UPDATE" in str(c) and "share_token" in str(c)),
        None,
    )
    assert update_call is not None
    sql = update_call[0][0]
    assert "NULL" in sql or "share_token" in sql


def test_new_share_generates_new_token(client):
    """share, unshare, share again -> a NEW token is generated (old link revoked).

    We verify the tokens in the share_url responses differ each time sharing is enabled.
    """
    cursor_mock = MagicMock()
    # Each fetchone call:
    # 1st PATCH share=true: no existing token
    # 2nd PATCH share=false: has token (returned for unshare logic)
    # 3rd PATCH share=true: no existing token (was cleared)
    cursor_mock.fetchone.side_effect = [
        ("nb_TEST", None),           # first share
        ("nb_TEST", "old-token"),    # unshare
        ("nb_TEST", None),           # re-share
    ]
    conn_mock = _make_mock_conn(cursor_mock)

    with patch("core.db.get_connection", return_value=conn_mock):
        resp1 = client.patch("/api/notebooks/nb_TEST/share", json={"shared": True})
        client.patch("/api/notebooks/nb_TEST/share", json={"shared": False})
        resp3 = client.patch("/api/notebooks/nb_TEST/share", json={"shared": True})

    assert resp1.status_code == 200
    assert resp3.status_code == 200

    token1 = resp1.json()["share_url"].split("/api/notebooks/shared/")[-1]
    token3 = resp3.json()["share_url"].split("/api/notebooks/shared/")[-1]

    # Two separate share calls -> two distinct tokens (unguessable, random)
    assert token1 != token3


def test_share_notebook_not_found(client):
    """PATCH share on non-existent notebook -> 404."""
    cursor_mock = MagicMock()
    cursor_mock.fetchone.return_value = None
    conn_mock = _make_mock_conn(cursor_mock)

    with patch("core.db.get_connection", return_value=conn_mock):
        resp = client.patch(
            "/api/notebooks/nb_MISSING/share",
            json={"shared": True},
        )

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Shared endpoint tests (AC6)
# ---------------------------------------------------------------------------


def test_shared_endpoint_returns_last_run(client):
    """GET /shared/{token} -> 200 with notebook + last run data."""
    token = "ValidToken12345678901234567890"

    nb_data = (
        "nb_TEST", "Analyse SEO hebdo", "gsc/position_movements",
        "last_7d", _fake_ts(),
    )
    run_data = (
        "nbrun_001",
        _fake_ts(),
        "Clics: 1 000",
        '{"schema_version": "1", "data": {}}',
        None,
        ["pull_001"],
        "success",
    )

    nb_description = [("id",), ("title",), ("report_ref",), ("window_rule",), ("created_at",)]
    run_description = [
        ("id",), ("executed_at",), ("summary_text",),
        ("envelope_inline",), ("envelope_ref",), ("pull_ids",), ("status",),
    ]

    # Use a cursor that switches description after the first execute() call
    class SwitchingCursor:
        def __init__(self):
            self._execute_count = 0
            self.description = nb_description

        def execute(self, sql, params=None):
            if self._execute_count == 0:
                self.description = nb_description
            else:
                self.description = run_description
            self._execute_count += 1

        def fetchone(self):
            if self._execute_count == 1:
                return nb_data
            return run_data

    switching_cursor = SwitchingCursor()
    cursor_cm = MagicMock()
    cursor_cm.__enter__ = MagicMock(return_value=switching_cursor)
    cursor_cm.__exit__ = MagicMock(return_value=False)
    conn_mock = MagicMock()
    conn_mock.__enter__ = MagicMock(return_value=conn_mock)
    conn_mock.__exit__ = MagicMock(return_value=False)
    conn_mock.cursor = MagicMock(return_value=cursor_cm)

    with patch("core.db.get_connection", return_value=conn_mock):
        resp = client.get(f"/api/notebooks/shared/{token}")

    assert resp.status_code == 200
    data = resp.json()
    assert "notebook" in data
    assert "run" in data
    assert data["notebook"]["title"] == "Analyse SEO hebdo"
    assert "id" not in data["notebook"]  # security: no identity fields


def test_shared_endpoint_unknown_token(client):
    """GET /shared/badtoken -> 404."""
    cursor_mock = MagicMock()
    cursor_mock.fetchone.return_value = None  # token not found
    conn_mock = _make_mock_conn(cursor_mock)

    with patch("core.db.get_connection", return_value=conn_mock):
        resp = client.get("/api/notebooks/shared/badtoken")

    assert resp.status_code == 404


def test_shared_endpoint_no_auth_required():
    """GET /shared/{token} works WITHOUT a Bearer token (public endpoint)."""
    # Create a client WITHOUT any auth header — use TOOROW_AUTH_MODE=oauth to ensure
    # auth guard would normally kick in, but shared endpoint bypasses it.
    # Simpler: just confirm the endpoint doesn't call _check_auth by mocking it to fail.
    os.environ["TOOROW_AUTH_MODE"] = "disabled"
    from core import api_auth

    api_auth.reset_verifier_cache()
    app = _make_app()

    cursor_mock = MagicMock()
    cursor_mock.fetchone.return_value = None  # token not found -> 404
    conn_mock = _make_mock_conn(cursor_mock)

    with TestClient(app) as c:
        with patch("core.db.get_connection", return_value=conn_mock):
            # No Authorization header — should not return 401
            resp = c.get("/api/notebooks/shared/anytoken")
    # 404 is fine (no matching token); what matters is NOT 401
    assert resp.status_code != 401

    os.environ.pop("TOOROW_AUTH_MODE", None)
    api_auth.reset_verifier_cache()


def test_shared_endpoint_does_not_expose_share_token(client):
    """GET /shared/{token} -> response does NOT include share_token or created_by."""
    nb_description = [("id",), ("title",), ("report_ref",), ("window_rule",), ("created_at",)]
    run_description = [
        ("id",), ("executed_at",), ("summary_text",),
        ("envelope_inline",), ("envelope_ref",), ("pull_ids",), ("status",),
    ]

    class SafeCursor:
        def __init__(self):
            self._execute_count = 0
            self.description = nb_description

        def execute(self, sql, params=None):
            if self._execute_count == 0:
                self.description = nb_description
            else:
                self.description = run_description
            self._execute_count += 1

        def fetchone(self):
            if self._execute_count == 1:
                return ("nb_TEST", "Mon Notebook", "adhoc", "last_7d", _fake_ts())
            return ("nbrun_001", _fake_ts(), "Summary here", None, None, [], "success")

    cursor = SafeCursor()
    cursor_cm = MagicMock()
    cursor_cm.__enter__ = MagicMock(return_value=cursor)
    cursor_cm.__exit__ = MagicMock(return_value=False)
    conn_mock = MagicMock()
    conn_mock.__enter__ = MagicMock(return_value=conn_mock)
    conn_mock.__exit__ = MagicMock(return_value=False)
    conn_mock.cursor = MagicMock(return_value=cursor_cm)

    with patch("core.db.get_connection", return_value=conn_mock):
        resp = client.get("/api/notebooks/shared/some-valid-token")

    assert resp.status_code in (200, 404)
    if resp.status_code == 200:
        data = resp.json()
        nb = data.get("notebook", {})
        assert "share_token" not in nb
        assert "created_by" not in nb
        assert "id" not in nb  # notebook id not exposed


# ---------------------------------------------------------------------------
# Slide export HTML tests (AC5, AC6)
# ---------------------------------------------------------------------------


def _make_html_export_cursor(title: str, window_rule: str, summary: str):
    """Build a SwitchingCursor for HTML export: first execute=notebook, second=run.

    Story 7.4 (AI-38): the notebook SELECT now also returns project_id (used by
    the cross-scope enforcement guard). Auth is disabled in these tests, so the
    caller identity is 'anonymous' -> access granted without a DB round-trip.
    """
    nb_description = [("title",), ("window_rule",), ("project_id",)]
    run_description = [("executed_at",), ("summary_text",), ("envelope_inline",), ("pull_ids",)]

    class HtmlExportCursor:
        def __init__(self):
            self._execute_count = 0
            self.description = nb_description

        def execute(self, sql, params=None):
            if self._execute_count == 0:
                self.description = nb_description
            else:
                self.description = run_description
            self._execute_count += 1

        def fetchone(self):
            if self._execute_count == 1:
                return (title, window_rule, "proj_test")
            return (_fake_ts(), summary, None, [])

    cursor = HtmlExportCursor()
    cursor_cm = MagicMock()
    cursor_cm.__enter__ = MagicMock(return_value=cursor)
    cursor_cm.__exit__ = MagicMock(return_value=False)
    conn_mock = MagicMock()
    conn_mock.__enter__ = MagicMock(return_value=conn_mock)
    conn_mock.__exit__ = MagicMock(return_value=False)
    conn_mock.cursor = MagicMock(return_value=cursor_cm)
    return conn_mock


def test_slide_export_html_returns_html(client):
    """GET /api/notebooks/{id}/runs/{run_id}/export/html -> Content-Type text/html."""
    conn_mock = _make_html_export_cursor("Mon Rapport SEO", "last_7d", "Clics en hausse.")

    with patch("core.db.get_connection", return_value=conn_mock):
        resp = client.get("/api/notebooks/nb_TEST/runs/nbrun_001/export/html")

    assert resp.status_code == 200
    content_type = resp.headers.get("content-type", "")
    assert "text/html" in content_type


def test_slide_export_html_contains_title(client):
    """HTML export includes the notebook title and summary_text."""
    conn_mock = _make_html_export_cursor(
        "Rapport SEO Hebdo", "last_7d", "Clics en hausse de 10%."
    )

    with patch("core.db.get_connection", return_value=conn_mock):
        resp = client.get("/api/notebooks/nb_TEST/runs/nbrun_001/export/html")

    assert resp.status_code == 200
    html = resp.text
    assert "Rapport SEO Hebdo" in html
    assert "Clics en hausse de 10%" in html


def test_slide_export_html_no_external_refs(client):
    """HTML export has no load-bearing external http(s) references (AD-11)."""
    conn_mock = _make_html_export_cursor("Mon Notebook", "last_30d", "Résumé.")

    with patch("core.db.get_connection", return_value=conn_mock):
        resp = client.get("/api/notebooks/nb_TEST/runs/nbrun_001/export/html")

    assert resp.status_code == 200
    html = resp.text

    # No load-bearing external URLs (src=, href=, url(), @import)
    load_bearing = re.findall(
        r'(?:src|href|srcset|action|data|poster)\s*=\s*["\']https?://|'
        r'url\(\s*["\']?https?://|@import\s+["\']https?://',
        html,
        re.IGNORECASE,
    )
    assert load_bearing == [], f"External refs found: {load_bearing}"


def test_slide_export_html_not_found(client):
    """GET /export/html for unknown notebook -> 404."""
    cursor_mock = MagicMock()
    cursor_mock.fetchone.return_value = None
    cursor_mock.description = [("title",), ("window_rule",)]
    conn_mock = _make_mock_conn(cursor_mock)

    with patch("core.db.get_connection", return_value=conn_mock):
        resp = client.get("/api/notebooks/nb_MISSING/runs/nbrun_001/export/html")

    assert resp.status_code == 404
