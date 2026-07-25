"""AI-56 seam tests for the Story 25.5 account-topology endpoints.

MANDATORY (Story 25.5 AC7): exercises the three new REST endpoints through the
REAL ``build_asgi_app()`` ASGI stack + Starlette TestClient (the G-14 convention),
NOT via direct handler calls. Asserts on VALUES (window list, refusal codes, scope
state), not just HTTP status -- the whole point of the AI-56 seam discipline.

  * GET  /api/connections/{id}/accounts   -- discovery hierarchy + 409 no-topology.
  * POST /api/connections/{id}/account     -- select + verify + trial enqueue (201).
  * POST /api/connections/{id}/backfill    -- windowing (202) + 422 invalid days.

Auth resolves to a test identity (patched _check_auth); project access is granted
(patched identity_has_project_access). The connection_ref SELECT is served by a
fake psycopg connection; the topology CORE functions are mocked so no live Nango /
GA Admin API / Postgres scope table is required (the endpoint<->core seam is what
this file covers -- the core logic has its own unit tests).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from starlette.testclient import TestClient  # noqa: E402

# ---------------------------------------------------------------------------
# Fake connection_ref SELECT: _resolve_conn_project_scoped reads (project_id, provider).
# ---------------------------------------------------------------------------


def _fake_conn_ref(project_id="proj_1", provider="google-analytics"):
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone = MagicMock(return_value=(project_id, provider))

    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cur)
    conn.commit = MagicMock()

    @contextmanager
    def _get_connection():
        yield conn

    return _get_connection


def _unknown_conn_ref():
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone = MagicMock(return_value=None)

    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cur)

    @contextmanager
    def _get_connection():
        yield conn

    return _get_connection


def _client():
    from core.main import build_asgi_app

    return TestClient(build_asgi_app(), raise_server_exceptions=True)


_AUTH_OK = patch(
    "core.admin_api._check_auth", new=AsyncMock(return_value=(True, "test@test"))
)


# ---------------------------------------------------------------------------
# GET /api/connections/{id}/accounts
# ---------------------------------------------------------------------------


def test_list_accounts_returns_hierarchy_through_asgi():
    accounts = [
        {"id": "accounts/1", "label": "Acme", "children": [
            {"id": "properties/9", "label": "Acme Web"},
        ]},
    ]
    discovered = {
        "topology": {"selection_level": "property"},
        "accounts": accounts,
    }
    with _AUTH_OK, \
        patch("core.db.get_connection", new=_fake_conn_ref()), \
        patch("core.project_access.identity_has_project_access", return_value=True), \
        patch("core.account_topology.discover_accounts", return_value=discovered):
        resp = _client().get(
            "/api/connections/conn_1/accounts", headers={"Host": "localhost"}
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["connection_ref_id"] == "conn_1"
    assert body["accounts"][0]["children"][0]["id"] == "properties/9"


def test_list_accounts_409_when_no_topology_through_asgi():
    from core.account_topology import NoTopologyError

    with _AUTH_OK, \
        patch("core.db.get_connection", new=_fake_conn_ref(provider="shopify")), \
        patch("core.project_access.identity_has_project_access", return_value=True), \
        patch("core.account_topology.discover_accounts",
              side_effect=NoTopologyError("shopify")):
        resp = _client().get(
            "/api/connections/conn_1/accounts", headers={"Host": "localhost"}
        )

    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "no_account_topology"


def test_list_accounts_403_cross_project_through_asgi():
    with _AUTH_OK, \
        patch("core.db.get_connection", new=_fake_conn_ref()), \
        patch("core.project_access.identity_has_project_access", return_value=False), \
        patch("core.admin_api.write_audit_row"):
        resp = _client().get(
            "/api/connections/conn_1/accounts", headers={"Host": "localhost"}
        )

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "forbidden"


# ---------------------------------------------------------------------------
# POST /api/connections/{id}/account
# ---------------------------------------------------------------------------


def test_select_account_verifies_and_enqueues_trial_through_asgi():
    scope = {
        "account_id": "properties/9",
        "account_label": "Acme Web",
        "state": "ready",
        "verified_at": "2026-07-21T00:00:00+00:00",
    }
    trial = {"job_id": "job_trial", "pull_id": "pull_trial", "state": "queued"}
    with _AUTH_OK, \
        patch("core.db.get_connection", new=_fake_conn_ref()), \
        patch("core.project_access.identity_has_project_access", return_value=True), \
        patch("core.admin_api.write_audit_row"), \
        patch("core.account_topology.verify_and_select_account", return_value=scope), \
        patch("core.account_topology.enqueue_trial_pull", return_value=trial):
        resp = _client().post(
            "/api/connections/conn_1/account",
            json={"account_id": "properties/9"},
            headers={"Host": "localhost"},
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["scope"]["state"] == "ready"
    assert body["scope"]["account_id"] == "properties/9"
    # AI-56: the trial pull was enqueued through the seam and surfaced.
    assert body["trial"]["job_id"] == "job_trial"


def test_select_account_422_when_account_id_missing():
    with _AUTH_OK, \
        patch("core.db.get_connection", new=_fake_conn_ref()), \
        patch("core.project_access.identity_has_project_access", return_value=True):
        resp = _client().post(
            "/api/connections/conn_1/account",
            json={},
            headers={"Host": "localhost"},
        )

    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "missing_field"


def test_select_account_422_when_not_reachable_through_asgi():
    from core.account_topology import AccountNotReachable

    with _AUTH_OK, \
        patch("core.db.get_connection", new=_fake_conn_ref()), \
        patch("core.project_access.identity_has_project_access", return_value=True), \
        patch("core.account_topology.verify_and_select_account",
              side_effect=AccountNotReachable("properties/UNKNOWN")):
        resp = _client().post(
            "/api/connections/conn_1/account",
            json={"account_id": "properties/UNKNOWN"},
            headers={"Host": "localhost"},
        )

    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "account_not_reachable"


# ---------------------------------------------------------------------------
# POST /api/connections/{id}/backfill
# ---------------------------------------------------------------------------


def test_backfill_returns_window_list_through_asgi():
    windows = [
        {"date_from": "2026-06-19", "date_to": "2026-07-19",
         "job_id": "job_1", "state": "queued", "deduplicated": False},
        {"date_from": "2026-07-20", "date_to": "2026-07-20",
         "job_id": "job_2", "state": "queued", "deduplicated": False},
    ]
    with _AUTH_OK, \
        patch("core.db.get_connection", new=_fake_conn_ref()), \
        patch("core.project_access.identity_has_project_access", return_value=True), \
        patch("core.account_topology.enqueue_backfill", return_value=windows):
        resp = _client().post(
            "/api/connections/conn_1/backfill",
            json={"days": 32},
            headers={"Host": "localhost"},
        )

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["days"] == 32
    # AI-56: value assertion -- 32 days -> exactly 2 windows, contiguous.
    assert len(body["windows"]) == 2
    assert body["windows"][0]["date_from"] == "2026-06-19"
    assert body["windows"][-1]["date_to"] == "2026-07-20"


def test_backfill_422_invalid_days_through_asgi():
    with _AUTH_OK, \
        patch("core.db.get_connection", new=_fake_conn_ref()), \
        patch("core.project_access.identity_has_project_access", return_value=True):
        resp = _client().post(
            "/api/connections/conn_1/backfill",
            json={"days": 366},
            headers={"Host": "localhost"},
        )

    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "invalid_days"


def test_backfill_422_invalid_days_validated_before_enqueue():
    """days=0 is rejected BEFORE any enqueue (enqueue_backfill must not be called)."""
    with _AUTH_OK, \
        patch("core.db.get_connection", new=_fake_conn_ref()), \
        patch("core.project_access.identity_has_project_access", return_value=True), \
        patch("core.account_topology.enqueue_backfill") as enq:
        resp = _client().post(
            "/api/connections/conn_1/backfill",
            json={"days": 0},
            headers={"Host": "localhost"},
        )

    assert resp.status_code == 422, resp.text
    enq.assert_not_called()


# ---------------------------------------------------------------------------
# Review F-1: legacy POST /api/connections/{id}/pull must surface the topology
# refusal dict as an actionable 409 (it used to KeyError into a 500).
# ---------------------------------------------------------------------------


def test_legacy_pull_returns_409_on_topology_refusal_through_asgi():
    refusal = {
        "state": "refused",
        "code": "account_not_selected",
        "message": "Select and verify a reporting account first.",
    }
    with _AUTH_OK, \
        patch("core.db.get_connection", new=_fake_conn_ref()), \
        patch("core.project_access.identity_has_project_access", return_value=True), \
        patch("core.queue.enqueue_pull", return_value=refusal):
        resp = _client().post(
            "/api/connections/conn_1/pull",
            json={"date_from": "2026-07-01", "date_to": "2026-07-03"},
            headers={"Host": "localhost"},
        )

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["code"] == "account_not_selected"
    assert "reporting account" in body["message"]
