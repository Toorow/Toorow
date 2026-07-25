"""ASGI seam tests for the Story 12.7 (observe) + 12.8 (managed-feed) admin_api
endpoints.

These are interface/seam tests (AI-56 build_asgi_app + TestClient), NOT the pg
integration tests that prove the ledger/observation constraints (those live in
server/tests/integration/*). They prove the HTTP wiring in admin_api.py:

  - auth gate (401 without a valid token),
  - role gate (non-disclosing 404 when the strict project role is missing),
  - cross-project scope (a wrong project_id -> non-disclosing 404 via the role gate,
    the plan-version scope for observe, or the ledger scope for managed-feed),
  - happy path with a faked connection + a mocked module seam (the module logic is
    proven elsewhere; here we prove the handler maps its result to the right HTTP),
  - the managed-feed error -> HTTP status mapping (409 / 422 / 404).

Strategy mirrors test_admin_api_cache.py:
  - build_asgi_app + TestClient(raise_server_exceptions=False).
  - AsyncMock _check_auth.
  - patch core.project_access.identity_has_project_role for the role gate.
  - patch core.db.get_connection with a fake conn (cursor -> scripted rows).
  - patch the module seams (observe_and_register / open_import / ...) so the DB is
    never really touched.

SCHEDULER_ENABLED=false, HEALTH_POLLER_ENABLED=false (usual pattern).
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_client():
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    return TestClient(build_asgi_app(), raise_server_exceptions=False)


def _auth_ok():
    return patch(
        "core.admin_api._check_auth",
        new=AsyncMock(return_value=(True, "test-user")),
    )


def _auth_fail():
    return patch(
        "core.admin_api._check_auth",
        new=AsyncMock(return_value=(False, None)),
    )


def _role(allowed: bool):
    """Patch the strict project-role check used by _require_datastream_role."""
    return patch(
        "core.project_access.identity_has_project_role",
        return_value=allowed,
    )


def _fake_conn(*, plan_payload: dict | None = None):
    """A fake psycopg-style connection whose cursor scripts fetchone/fetchall.

    ``plan_payload`` (when given) is returned as the single fetchone() column so the
    observe handler's inline plan-version SELECT resolves source.external_object.
    """
    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.execute = MagicMock(return_value=None)
    if plan_payload is not None:
        cursor.fetchone = MagicMock(return_value=(plan_payload,))
    else:
        cursor.fetchone = MagicMock(return_value=None)
    cursor.fetchall = MagicMock(return_value=[])

    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cursor)
    conn.commit = MagicMock()
    conn.rollback = MagicMock()

    @contextmanager
    def _get_connection():
        yield conn

    return conn, _get_connection


_HDR = {"Authorization": "Bearer test-secret"}


# ---------------------------------------------------------------------------
# 12.7 -- POST /api/datastreams/{id}/observe
# ---------------------------------------------------------------------------


class TestObserveEndpoint:
    _EXTERNAL_OBJECT = {
        "project": "gcp-proj",
        "dataset": "ds",
        "object": "tbl",
        "writer_identity": "ext@pipeline",
    }
    _PLAN_PAYLOAD = {"source": {"kind": "external_bq", "external_object": _EXTERNAL_OBJECT}}

    def _body(self):
        return {
            "project_id": "proj_alpha",
            "plan_version_id": "pv_1",
            "mapping_version_id": "mv_1",
            "projection_plan": {"select": []},
            "probe_result": {
                "exists": True,
                "readable": True,
                "row_count": 10,
                "schema_hash": "s" * 64,
                "object_location": "EU",
                "sample_rows": [{"a": 1}],
            },
            "idempotency_key": "idem-1",
        }

    def test_401_without_auth(self):
        with _auth_fail():
            client = _build_client()
            resp = client.post("/api/datastreams/ds_1/observe", json=self._body())
        assert resp.status_code == 401

    def test_role_denied_returns_non_disclosing_404(self):
        _conn, fake_get = _fake_conn(plan_payload=self._PLAN_PAYLOAD)
        with (
            _auth_ok(),
            _role(False),
            patch("core.db.get_connection", new=fake_get),
        ):
            client = _build_client()
            resp = client.post("/api/datastreams/ds_1/observe", headers=_HDR, json=self._body())
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_unknown_plan_version_returns_404(self):
        # No external_object resolvable (fetchone -> None) -> non-disclosing 404.
        _conn, fake_get = _fake_conn(plan_payload=None)
        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
        ):
            client = _build_client()
            resp = client.post("/api/datastreams/ds_1/observe", headers=_HDR, json=self._body())
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_ok_verdict_returns_201_with_execution(self):
        conn, fake_get = _fake_conn(plan_payload=self._PLAN_PAYLOAD)
        seam_result = {
            "verdict": "ok",
            "observation": {"id": "ebqo_1", "verdict": "ok", "repair": {}},
            "execution": {"id": "dse_1", "state": "created"},
            "virtual_pull_commit": {"pull_id": "vpull_abc"},
        }
        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch(
                "core.external_bq_registration.observe_and_register",
                return_value=seam_result,
            ) as seam,
        ):
            client = _build_client()
            resp = client.post("/api/datastreams/ds_1/observe", headers=_HDR, json=self._body())
        assert resp.status_code == 201
        data = resp.json()
        assert data["verdict"] == "ok"
        assert data["execution"]["id"] == "dse_1"
        assert data["virtual_pull_commit"]["pull_id"] == "vpull_abc"
        # external_object is resolved server-side and passed to the seam (not the body).
        assert seam.call_args.kwargs["external_object"] == self._EXTERNAL_OBJECT
        conn.commit.assert_called()

    def test_blocking_verdict_returns_200_with_repair(self):
        _conn, fake_get = _fake_conn(plan_payload=self._PLAN_PAYLOAD)
        seam_result = {
            "verdict": "absent",
            "observation": {
                "id": "ebqo_2",
                "verdict": "absent",
                "repair": {"code": "external_object_absent"},
            },
        }
        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch(
                "core.external_bq_registration.observe_and_register",
                return_value=seam_result,
            ),
        ):
            client = _build_client()
            resp = client.post("/api/datastreams/ds_1/observe", headers=_HDR, json=self._body())
        assert resp.status_code == 200
        data = resp.json()
        assert data["verdict"] == "absent"
        assert data["repair"]["code"] == "external_object_absent"

    def test_unchanged_noop_returns_200_no_execution(self):
        _conn, fake_get = _fake_conn(plan_payload=self._PLAN_PAYLOAD)
        seam_result = {
            "verdict": "unchanged_noop",
            "observation": {"id": "ebqo_3", "verdict": "unchanged_noop", "repair": {}},
        }
        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch(
                "core.external_bq_registration.observe_and_register",
                return_value=seam_result,
            ),
        ):
            client = _build_client()
            resp = client.post("/api/datastreams/ds_1/observe", headers=_HDR, json=self._body())
        assert resp.status_code == 200
        data = resp.json()
        assert data["verdict"] == "unchanged_noop"
        assert "execution" not in data

    def test_missing_fields_returns_422(self):
        _conn, fake_get = _fake_conn(plan_payload=self._PLAN_PAYLOAD)
        body = self._body()
        del body["probe_result"]
        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
        ):
            client = _build_client()
            resp = client.post("/api/datastreams/ds_1/observe", headers=_HDR, json=body)
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 12.8 -- managed-feed imports
# ---------------------------------------------------------------------------


class TestOpenManagedFeedImport:
    def _body(self):
        return {
            "project_id": "proj_alpha",
            "plan_version_id": "pv_1",
            "mapping_version_id": "mv_1",
            "feed_format": "csv",
            "projection_plan": {"select": []},
            "idempotency_key": "idem-1",
            "source_metadata": {"upload_slot": "u1"},
        }

    def test_401_without_auth(self):
        with _auth_fail():
            client = _build_client()
            resp = client.post("/api/datastreams/ds_1/managed-feed/imports", json=self._body())
        assert resp.status_code == 401

    def test_role_denied_returns_404(self):
        _conn, fake_get = _fake_conn()
        with (
            _auth_ok(),
            _role(False),
            patch("core.db.get_connection", new=fake_get),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/managed-feed/imports",
                headers=_HDR,
                json=self._body(),
            )
        assert resp.status_code == 404

    def test_happy_path_returns_201(self):
        conn, fake_get = _fake_conn()
        seam_result = {
            "ledger": {"id": "mfl_1", "outcome": "opened"},
            "execution": {"id": "dse_1"},
            "no_op": False,
            "replay": False,
        }
        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch("core.managed_feed_ledger.open_import", return_value=seam_result),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/managed-feed/imports",
                headers=_HDR,
                json=self._body(),
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["ledger"]["id"] == "mfl_1"
        assert data["replay"] is False
        conn.commit.assert_called()

    def test_payload_conflict_returns_409(self):
        _conn, fake_get = _fake_conn()
        from core.managed_feed_ledger import ImportPayloadConflict

        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch(
                "core.managed_feed_ledger.open_import",
                side_effect=ImportPayloadConflict(),
            ),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/managed-feed/imports",
                headers=_HDR,
                json=self._body(),
            )
        assert resp.status_code == 409
        assert resp.json()["code"] == "import_payload_conflict"

    def test_invalid_format_returns_422(self):
        _conn, fake_get = _fake_conn()
        from core.managed_feed_ledger import InvalidFeedFormat

        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch(
                "core.managed_feed_ledger.open_import",
                side_effect=InvalidFeedFormat("unknown feed_format 'xml'"),
            ),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/managed-feed/imports",
                headers=_HDR,
                json=self._body(),
            )
        assert resp.status_code == 422
        assert resp.json()["code"] == "invalid_feed_format"

    def test_import_in_progress_returns_409(self):
        _conn, fake_get = _fake_conn()
        from core.managed_feed_ledger import ImportInProgress

        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch(
                "core.managed_feed_ledger.open_import",
                side_effect=ImportInProgress(),
            ),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/managed-feed/imports",
                headers=_HDR,
                json=self._body(),
            )
        assert resp.status_code == 409
        assert resp.json()["code"] == "managed_feed_import_in_progress"


class TestRecordManagedFeedRows:
    def _body(self):
        return {
            "project_id": "proj_alpha",
            "landing_relation": "org_x_raw.managed_feed_ds_1",
            "accepted_row_count": 5,
            "content_hash": "a" * 64,
            "rejected_rows": [],
        }

    def test_role_denied_returns_404(self):
        _conn, fake_get = _fake_conn()
        with (
            _auth_ok(),
            _role(False),
            patch("core.db.get_connection", new=fake_get),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/managed-feed/imports/mfl_1/rows",
                headers=_HDR,
                json=self._body(),
            )
        assert resp.status_code == 404

    def test_happy_path_returns_200(self):
        conn, fake_get = _fake_conn()
        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch(
                "core.managed_feed_ledger.record_rows",
                return_value={"id": "mfl_1", "outcome": "written", "row_count": 5},
            ),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/managed-feed/imports/mfl_1/rows",
                headers=_HDR,
                json=self._body(),
            )
        assert resp.status_code == 200
        assert resp.json()["outcome"] == "written"
        conn.commit.assert_called()

    def test_terminal_ledger_returns_409(self):
        _conn, fake_get = _fake_conn()
        from core.managed_feed_ledger import LedgerTerminal

        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch(
                "core.managed_feed_ledger.record_rows",
                side_effect=LedgerTerminal("published"),
            ),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/managed-feed/imports/mfl_1/rows",
                headers=_HDR,
                json=self._body(),
            )
        assert resp.status_code == 409
        assert resp.json()["code"] == "managed_feed_ledger_terminal"

    def test_content_hash_mismatch_returns_422(self):
        _conn, fake_get = _fake_conn()
        from core.managed_feed_ledger import ManagedFeedError

        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch(
                "core.managed_feed_ledger.record_rows",
                side_effect=ManagedFeedError("content_hash_mismatch", "changed"),
            ),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/managed-feed/imports/mfl_1/rows",
                headers=_HDR,
                json=self._body(),
            )
        assert resp.status_code == 422
        assert resp.json()["code"] == "content_hash_mismatch"


class TestPublishManagedFeedImport:
    def test_role_denied_returns_404(self):
        _conn, fake_get = _fake_conn()
        with (
            _auth_ok(),
            _role(False),
            patch("core.db.get_connection", new=fake_get),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/managed-feed/imports/mfl_1/publish",
                headers=_HDR,
                json={"project_id": "proj_alpha"},
            )
        assert resp.status_code == 404

    def test_rejection_gate_blocks_with_422(self):
        _conn, fake_get = _fake_conn()
        issue = {"code": "rejection_threshold_exceeded", "detail": "40% > 25%"}
        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch(
                "core.managed_feed_ledger.evaluate_rejection_gate_for_ledger",
                return_value=issue,
            ),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/managed-feed/imports/mfl_1/publish",
                headers=_HDR,
                json={"project_id": "proj_alpha"},
            )
        assert resp.status_code == 422
        data = resp.json()
        assert data["code"] == "rejection_threshold_exceeded"
        assert data["issue"]["detail"] == "40% > 25%"

    def test_happy_path_publishes_and_marks_outcome(self):
        conn, fake_get = _fake_conn()
        # After the rejection gate passes, the handler reads the execution state via
        # the inline SELECT; return 'ready' so no advance is attempted.
        conn.cursor.return_value.fetchone = MagicMock(return_value=("ready",))
        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch(
                "core.managed_feed_ledger.evaluate_rejection_gate_for_ledger",
                return_value=None,
            ),
            patch(
                "core.managed_feed_ledger.get_ledger",
                return_value={"id": "mfl_1", "execution_id": "dse_1", "outcome": "written"},
            ),
            patch("core.datastream_publication.run_dq_gates", return_value=[]),
            patch("core.datastream_publication.commit_publication", return_value={}),
            patch(
                "core.managed_feed_ledger.mark_outcome",
                return_value={"id": "mfl_1", "outcome": "published"},
            ) as mark,
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/managed-feed/imports/mfl_1/publish",
                headers=_HDR,
                json={"project_id": "proj_alpha"},
            )
        assert resp.status_code == 200
        assert resp.json()["outcome"] == "published"
        assert mark.call_args.args[2] == "published"

    def test_dq_gate_failure_returns_422(self):
        conn, fake_get = _fake_conn()
        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch(
                "core.managed_feed_ledger.evaluate_rejection_gate_for_ledger",
                return_value=None,
            ),
            patch(
                "core.managed_feed_ledger.get_ledger",
                return_value={"id": "mfl_1", "execution_id": "dse_1", "outcome": "written"},
            ),
            patch(
                "core.datastream_publication.run_dq_gates",
                return_value=[{"code": "row_count_delta", "detail": "drop"}],
            ),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/managed-feed/imports/mfl_1/publish",
                headers=_HDR,
                json={"project_id": "proj_alpha"},
            )
        assert resp.status_code == 422
        assert resp.json()["code"] == "dq_gate_failed"


class TestManagedFeedReads:
    def test_list_role_denied_returns_404(self):
        _conn, fake_get = _fake_conn()
        with (
            _auth_ok(),
            _role(False),
            patch("core.db.get_connection", new=fake_get),
        ):
            client = _build_client()
            resp = client.get(
                "/api/datastreams/ds_1/managed-feed/imports?project_id=proj_alpha",
                headers=_HDR,
            )
        assert resp.status_code == 404

    def test_list_happy_path(self):
        _conn, fake_get = _fake_conn()
        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch(
                "core.managed_feed_ledger.list_ledger",
                return_value=[{"id": "mfl_1"}, {"id": "mfl_2"}],
            ),
        ):
            client = _build_client()
            resp = client.get(
                "/api/datastreams/ds_1/managed-feed/imports?project_id=proj_alpha",
                headers=_HDR,
            )
        assert resp.status_code == 200
        assert len(resp.json()["imports"]) == 2

    def test_get_missing_project_id_returns_400(self):
        with _auth_ok():
            client = _build_client()
            resp = client.get("/api/datastreams/ds_1/managed-feed/imports/mfl_1", headers=_HDR)
        assert resp.status_code == 400

    def test_get_not_found_returns_404(self):
        _conn, fake_get = _fake_conn()
        from core.managed_feed_ledger import LedgerNotFound

        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch(
                "core.managed_feed_ledger.get_ledger",
                side_effect=LedgerNotFound(),
            ),
        ):
            client = _build_client()
            resp = client.get(
                "/api/datastreams/ds_1/managed-feed/imports/mfl_1?project_id=proj_alpha",
                headers=_HDR,
            )
        assert resp.status_code == 404

    def test_rejected_rows_happy_path(self):
        _conn, fake_get = _fake_conn()
        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch(
                "core.managed_feed_ledger.get_rejected_rows",
                return_value=[{"row_number": 3, "field_name": "amount"}],
            ) as seam,
        ):
            client = _build_client()
            resp = client.get(
                "/api/datastreams/ds_1/managed-feed/imports/mfl_1/rejected-rows"
                "?project_id=proj_alpha&limit=50&offset=10",
                headers=_HDR,
            )
        assert resp.status_code == 200
        assert resp.json()["rejected_rows"][0]["field_name"] == "amount"
        # limit/offset parsed and forwarded to the seam.
        assert seam.call_args.kwargs["limit"] == 50
        assert seam.call_args.kwargs["offset"] == 10


def test_json_module_importable():
    # Guard: the handlers rely on json (already imported at module scope).
    assert json.dumps({"ok": True}) == '{"ok": true}'
