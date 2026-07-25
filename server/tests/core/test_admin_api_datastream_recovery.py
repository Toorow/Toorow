"""ASGI seam tests for the Story 12.9 / 12.10 / 12.11 / 12.12 / 12.14 admin_api
endpoints wired on top of the datastream surface.

These are interface/seam tests (build_asgi_app + TestClient), NOT the pg
integration tests that prove the module invariants (those live in
server/tests/integration/*). They prove the HTTP wiring in admin_api.py for each
new route group:

  - auth gate (401 without a valid token),
  - role gate (non-disclosing 404 when the strict project role is missing),
  - cross-project scope (a wrong project_id -> non-disclosing 404 via the role gate),
  - a happy path with a faked connection + a patched module seam (the module logic
    is proven elsewhere; here we prove the handler maps its result to the right HTTP),
  - the module error -> HTTP status mapping (409 / 422 / 403 / 404).

Strategy mirrors test_admin_api_datastream_feeds.py:
  - build_asgi_app + TestClient(raise_server_exceptions=False),
  - AsyncMock _check_auth,
  - patch core.project_access.identity_has_project_role for the role gate,
  - patch core.db.get_connection with a fake conn (cursor -> scripted rows),
  - patch the module seams (build_preview / run_import / run_sync / ...) so the DB
    is never really touched.

The admin_api import is slow; this file is self-contained (no shared conftest seam).
"""

from __future__ import annotations

import base64
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


def _fake_conn(*, fetchone=None, fetchall=None):
    """A fake psycopg-style connection whose cursor scripts fetchone/fetchall."""
    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.execute = MagicMock(return_value=None)
    cursor.fetchone = MagicMock(return_value=fetchone)
    cursor.fetchall = MagicMock(return_value=fetchall or [])
    cursor.description = []

    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cursor)
    conn.commit = MagicMock()
    conn.rollback = MagicMock()

    @contextmanager
    def _get_connection():
        yield conn

    return conn, _get_connection


_HDR = {"Authorization": "Bearer test-secret"}
_B64 = base64.b64encode(b"date,amount\n2026-01-01,10\n").decode()


# ---------------------------------------------------------------------------
# 12.9 -- CSV / Excel governed import
# ---------------------------------------------------------------------------


class TestCsvExcelPreview:
    def _body(self):
        return {"project_id": "proj_alpha", "file_base64": _B64, "filename": "b.csv"}

    def test_401_without_auth(self):
        with _auth_fail():
            client = _build_client()
            resp = client.post("/api/datastreams/ds_1/imports/preview", json=self._body())
        assert resp.status_code == 401

    def test_role_denied_returns_404(self):
        _conn, fake_get = _fake_conn()
        with _auth_ok(), _role(False), patch("core.db.get_connection", new=fake_get):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/imports/preview", headers=_HDR, json=self._body()
            )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_missing_file_returns_422(self):
        _conn, fake_get = _fake_conn()
        body = self._body()
        del body["file_base64"]
        with _auth_ok(), _role(True), patch("core.db.get_connection", new=fake_get):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/imports/preview", headers=_HDR, json=body
            )
        assert resp.status_code == 422
        assert resp.json()["code"] == "missing_field"

    def test_happy_path_returns_200(self):
        from dataclasses import dataclass, field

        @dataclass
        class _Preview:
            format: str = "csv"
            encoding: str = "utf-8"
            delimiter: str = ","
            sheet_name: str | None = None
            columns: list = field(default_factory=list)
            row_count: int = 1
            rejected_count: int = 0
            preview_rows: list = field(default_factory=lambda: [{"date": "2026-01-01"}])
            content_hash: str = "a" * 64
            contract_version_id: str | None = None

        _conn, fake_get = _fake_conn()
        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch("core.csv_excel_import.build_preview", return_value=_Preview()),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/imports/preview", headers=_HDR, json=self._body()
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["format"] == "csv"
        assert data["row_count"] == 1

    def test_parse_error_returns_422(self):
        _conn, fake_get = _fake_conn()
        from core.csv_excel_import import DuplicateColumns

        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch(
                "core.csv_excel_import.build_preview",
                side_effect=DuplicateColumns("dup header 'x'"),
            ),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/imports/preview", headers=_HDR, json=self._body()
            )
        assert resp.status_code == 422
        assert resp.json()["code"] == "duplicate_columns"


class TestCsvExcelConfirm:
    def _body(self, **over):
        body = {
            "project_id": "proj_alpha",
            "plan_version_id": "dsp_1",
            "mapping_version_id": "dmap_1",
            "projection_plan": {"executable": True},
            "source_metadata": {"filename": "b.csv"},
            "contract": {"format": "csv"},
            "idempotency_key": "idem-1",
            "file_base64": _B64,
        }
        body.update(over)
        return body

    def test_401_without_auth(self):
        with _auth_fail():
            client = _build_client()
            resp = client.post("/api/datastreams/ds_1/imports", json=self._body())
        assert resp.status_code == 401

    def test_role_denied_returns_404(self):
        _conn, fake_get = _fake_conn()
        with _auth_ok(), _role(False), patch("core.db.get_connection", new=fake_get):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/imports", headers=_HDR, json=self._body()
            )
        assert resp.status_code == 404

    def test_happy_path_returns_200(self):
        conn, fake_get = _fake_conn(fetchone=(False,))  # allow_empty_publication read
        result = {"outcome": "written_pending_publication", "published": False, "blocked": False}
        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch("core.csv_excel_import.run_import", return_value=result) as seam,
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/imports", headers=_HDR, json=self._body()
            )
        assert resp.status_code == 200
        assert resp.json()["outcome"] == "written_pending_publication"
        conn.commit.assert_called()
        # The confirm route decodes the base64 and forwards the raw bytes.
        assert seam.call_args.args[0] == b"date,amount\n2026-01-01,10\n"

    def test_append_unavailable_returns_422(self):
        _conn, fake_get = _fake_conn(fetchone=(False,))
        from core.csv_excel_import import AppendUnavailable

        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch("core.csv_excel_import.run_import", side_effect=AppendUnavailable()),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/imports", headers=_HDR, json=self._body()
            )
        assert resp.status_code == 422
        assert resp.json()["code"] == "append_unavailable"

    def test_payload_conflict_returns_409(self):
        _conn, fake_get = _fake_conn(fetchone=(False,))
        from core.managed_feed_ledger import ImportPayloadConflict

        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch(
                "core.csv_excel_import.run_import",
                side_effect=ImportPayloadConflict(),
            ),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/imports", headers=_HDR, json=self._body()
            )
        assert resp.status_code == 409

    def test_force_empty_publish_requires_owner(self):
        # force_empty_publish -> Owner floor; a member (role check returns False for
        # 'owner') gets a non-disclosing 404.
        _conn, fake_get = _fake_conn(fetchone=(False,))
        with _auth_ok(), _role(False), patch("core.db.get_connection", new=fake_get):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/imports",
                headers=_HDR,
                json=self._body(force_empty_publish=True),
            )
        assert resp.status_code == 404


class TestImportContracts:
    def test_put_role_denied_returns_404(self):
        _conn, fake_get = _fake_conn()
        with _auth_ok(), _role(False), patch("core.db.get_connection", new=fake_get):
            client = _build_client()
            resp = client.put(
                "/api/datastreams/ds_1/import-contracts",
                headers=_HDR,
                json={"project_id": "proj_alpha", "contract": {"format": "csv"}},
            )
        assert resp.status_code == 404

    def test_put_happy_path_returns_200(self):
        conn, fake_get = _fake_conn()
        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch("core.csv_excel_import.version_contract", return_value="cic_1"),
        ):
            client = _build_client()
            resp = client.put(
                "/api/datastreams/ds_1/import-contracts",
                headers=_HDR,
                json={"project_id": "proj_alpha", "contract": {"format": "csv"}},
            )
        assert resp.status_code == 200
        assert resp.json()["import_contract_id"] == "cic_1"
        conn.commit.assert_called()

    def test_put_invalid_contract_returns_422(self):
        conn, fake_get = _fake_conn()
        from core.csv_excel_import import InvalidImportContract

        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch(
                "core.csv_excel_import.version_contract",
                side_effect=InvalidImportContract("bad delimiter"),
            ),
        ):
            client = _build_client()
            resp = client.put(
                "/api/datastreams/ds_1/import-contracts",
                headers=_HDR,
                json={"project_id": "proj_alpha", "contract": {"format": "csv"}},
            )
        assert resp.status_code == 422
        assert resp.json()["code"] == "invalid_import_contract"

    def test_list_missing_project_id_returns_400(self):
        with _auth_ok():
            client = _build_client()
            resp = client.get("/api/datastreams/ds_1/import-contracts", headers=_HDR)
        assert resp.status_code == 400

    def test_get_not_found_returns_404(self):
        cursor_conn, fake_get = _fake_conn(fetchone=None)
        with _auth_ok(), _role(True), patch("core.db.get_connection", new=fake_get):
            client = _build_client()
            resp = client.get(
                "/api/datastreams/ds_1/import-contracts/cic_x?project_id=proj_alpha",
                headers=_HDR,
            )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 12.10 -- Google Sheets recurring sync
# ---------------------------------------------------------------------------


class TestConfigureManagedFeedSync:
    def _body(self, **over):
        body = {
            "project_id": "proj_alpha",
            "connection_id": "conn_1",
            "spreadsheet_id": "1Bxi",
            "sheet_range": "Budget!A:F",
            "cadence_mode": "daily",
        }
        body.update(over)
        return body

    def test_401_without_auth(self):
        with _auth_fail():
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/managed-feed/configure", json=self._body()
            )
        assert resp.status_code == 401

    def test_role_denied_returns_404(self):
        _conn, fake_get = _fake_conn()
        with _auth_ok(), _role(False), patch("core.db.get_connection", new=fake_get):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/managed-feed/configure",
                headers=_HDR,
                json=self._body(),
            )
        assert resp.status_code == 404

    def test_hourly_without_quota_returns_422(self):
        # validate_cadence fires BEFORE the DB write (and before the role gate reads).
        _conn, fake_get = _fake_conn()
        with _auth_ok(), _role(True), patch("core.db.get_connection", new=fake_get):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/managed-feed/configure",
                headers=_HDR,
                json=self._body(cadence_mode="hourly", quota_profile={"allow_hourly": False}),
            )
        assert resp.status_code == 422
        assert resp.json()["code"] == "quota_hourly_not_permitted"

    def test_happy_path_returns_201(self):
        # The RETURNING row is scripted on fetchone with a description of the columns.
        cols = [
            "datastream_id", "project_id", "connection_id", "spreadsheet_id",
            "sheet_range", "sheet_name", "column_mapping", "cadence_mode",
            "cadence_policy", "quota_profile", "last_sync_at", "last_ledger_id",
            "last_watermark", "enabled", "created_by", "created_at", "updated_at",
        ]
        row = (
            "ds_1", "proj_alpha", "conn_1", "1Bxi", "Budget!A:F", "", {}, "daily",
            {}, {}, None, None, None, True, "test-user", None, None,
        )
        conn, fake_get = _fake_conn(fetchone=row)
        conn.cursor.return_value.description = [(c,) for c in cols]
        with _auth_ok(), _role(True), patch("core.db.get_connection", new=fake_get):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/managed-feed/configure",
                headers=_HDR,
                json=self._body(),
            )
        assert resp.status_code == 201
        assert resp.json()["cadence_mode"] == "daily"
        conn.commit.assert_called()


class TestSyncNowManagedFeed:
    _SCHEDULE = {
        "datastream_id": "ds_1",
        "project_id": "proj_alpha",
        "connection_id": "conn_1",
        "spreadsheet_id": "1Bxi",
        "sheet_range": "Budget!A:F",
        "sheet_name": "Budget",
        "column_mapping": {},
        "cadence_mode": "manual",
        "quota_profile": None,
    }

    def test_401_without_auth(self):
        with _auth_fail():
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/managed-feed/sync-now",
                json={"project_id": "proj_alpha"},
            )
        assert resp.status_code == 401

    def test_role_denied_returns_404(self):
        _conn, fake_get = _fake_conn()
        with _auth_ok(), _role(False), patch("core.db.get_connection", new=fake_get):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/managed-feed/sync-now",
                headers=_HDR,
                json={"project_id": "proj_alpha"},
            )
        assert resp.status_code == 404

    def test_no_schedule_returns_404(self):
        _conn, fake_get = _fake_conn()
        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch("core.admin_api._fetch_sync_schedule", return_value=None),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/managed-feed/sync-now",
                headers=_HDR,
                json={"project_id": "proj_alpha"},
            )
        assert resp.status_code == 404

    def test_happy_path_returns_200(self):
        conn, fake_get = _fake_conn()
        result = {"outcome": "validated_pending_publication", "ledger_id": "mfl_1"}
        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch("core.admin_api._fetch_sync_schedule", return_value=self._SCHEDULE),
            patch("core.google_sheets_sync.run_sync", return_value=result),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/managed-feed/sync-now",
                headers=_HDR,
                json={"project_id": "proj_alpha"},
            )
        assert resp.status_code == 200
        assert resp.json()["outcome"] == "validated_pending_publication"

    def test_phase_b_live_blocked_returns_503(self):
        _conn, fake_get = _fake_conn()
        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch("core.admin_api._fetch_sync_schedule", return_value=self._SCHEDULE),
            patch(
                "core.google_sheets_sync.run_sync",
                side_effect=NotImplementedError("PHASE_B_LIVE_BLOCKED: adapter"),
            ),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/managed-feed/sync-now",
                headers=_HDR,
                json={"project_id": "proj_alpha"},
            )
        assert resp.status_code == 503
        assert resp.json()["code"] == "phase_b_live_blocked"

    def test_quota_violation_returns_422(self):
        _conn, fake_get = _fake_conn()
        from core.google_sheets_sync import QuotaViolation

        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch("core.admin_api._fetch_sync_schedule", return_value=self._SCHEDULE),
            patch(
                "core.google_sheets_sync.run_sync",
                side_effect=QuotaViolation("hourly blocked"),
            ),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/managed-feed/sync-now",
                headers=_HDR,
                json={"project_id": "proj_alpha"},
            )
        assert resp.status_code == 422


class TestStatusManagedFeedSync:
    def test_missing_project_id_returns_400(self):
        with _auth_ok():
            client = _build_client()
            resp = client.get("/api/datastreams/ds_1/managed-feed/status", headers=_HDR)
        assert resp.status_code == 400

    def test_role_denied_returns_404(self):
        _conn, fake_get = _fake_conn()
        with _auth_ok(), _role(False), patch("core.db.get_connection", new=fake_get):
            client = _build_client()
            resp = client.get(
                "/api/datastreams/ds_1/managed-feed/status?project_id=proj_alpha",
                headers=_HDR,
            )
        assert resp.status_code == 404

    def test_happy_path_returns_200(self):
        schedule = {"cadence_mode": "manual", "cadence_policy": {}, "last_watermark": None}
        _conn, fake_get = _fake_conn()
        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch("core.admin_api._fetch_sync_schedule", return_value=schedule),
            patch("core.managed_feed_ledger.list_ledger", return_value=[{"id": "mfl_1"}]),
            patch(
                "core.google_sheets_sync.describe_next_run",
                return_value={"next_run_at": None},
            ),
            patch(
                "core.datastream_schedule.calculate_schedule_window",
                return_value=None,
            ),
        ):
            client = _build_client()
            resp = client.get(
                "/api/datastreams/ds_1/managed-feed/status?project_id=proj_alpha",
                headers=_HDR,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["schedule"]["cadence_mode"] == "manual"
        assert data["last_runs"][0]["id"] == "mfl_1"


# ---------------------------------------------------------------------------
# 12.11 -- bounded sync / reload / reprocess
# ---------------------------------------------------------------------------


class TestPrepareBoundedRecovery:
    def _body(self, **over):
        body = {"project_id": "proj_alpha", "kind": "synchronize"}
        body.update(over)
        return body

    def test_401_without_auth(self):
        with _auth_fail():
            client = _build_client()
            resp = client.post("/api/datastreams/ds_1/bounded/prepare", json=self._body())
        assert resp.status_code == 401

    def test_role_denied_returns_404(self):
        _conn, fake_get = _fake_conn()
        with _auth_ok(), _role(False), patch("core.db.get_connection", new=fake_get):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/bounded/prepare", headers=_HDR, json=self._body()
            )
        assert resp.status_code == 404

    def test_unknown_datastream_returns_404(self):
        # org_id read returns None -> non-disclosing 404.
        _conn, fake_get = _fake_conn(fetchone=None)
        with _auth_ok(), _role(True), patch("core.db.get_connection", new=fake_get):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/bounded/prepare", headers=_HDR, json=self._body()
            )
        assert resp.status_code == 404

    def test_happy_path_returns_200(self):
        conn, fake_get = _fake_conn(fetchone=("org_1",))
        result = {"preparation_id": "prep_1", "kind": "synchronize", "interval": {}}
        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch("core.bounded_recovery.prepare_bounded_recovery", return_value=result) as seam,
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/bounded/prepare", headers=_HDR, json=self._body()
            )
        assert resp.status_code == 200
        assert resp.json()["preparation_id"] == "prep_1"
        assert seam.call_args.kwargs["org_id"] == "org_1"
        conn.commit.assert_called()

    def test_forbidden_interval_returns_422(self):
        _conn, fake_get = _fake_conn(fetchone=("org_1",))
        from core.bounded_recovery import BoundedRecoveryError

        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch(
                "core.bounded_recovery.prepare_bounded_recovery",
                side_effect=BoundedRecoveryError("forbidden_interval", "trop large"),
            ),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/bounded/prepare",
                headers=_HDR,
                json=self._body(kind="reload"),
            )
        assert resp.status_code == 422
        assert resp.json()["code"] == "forbidden_interval"

    def test_lock_conflict_returns_409(self):
        _conn, fake_get = _fake_conn(fetchone=("org_1",))
        from core.bounded_recovery import BoundedRecoveryError

        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch(
                "core.bounded_recovery.prepare_bounded_recovery",
                side_effect=BoundedRecoveryError("lock_conflict", "active"),
            ),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/bounded/prepare", headers=_HDR, json=self._body()
            )
        assert resp.status_code == 409


class TestConfirmBoundedRecovery:
    def test_missing_preparation_id_returns_422(self):
        _conn, fake_get = _fake_conn(fetchone=("org_1",))
        with _auth_ok(), _role(True), patch("core.db.get_connection", new=fake_get):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/bounded/confirm",
                headers=_HDR,
                json={"project_id": "proj_alpha"},
            )
        assert resp.status_code == 422

    def test_role_denied_returns_404(self):
        _conn, fake_get = _fake_conn()
        with _auth_ok(), _role(False), patch("core.db.get_connection", new=fake_get):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/bounded/confirm",
                headers=_HDR,
                json={"project_id": "proj_alpha", "preparation_id": "prep_1"},
            )
        assert resp.status_code == 404

    def test_happy_path_returns_200(self):
        conn, fake_get = _fake_conn(fetchone=("org_1",))
        result = {"preparation_id": "prep_1", "operation_id": "op_1", "outcome": "succeeded"}
        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch("core.bounded_recovery.confirm_bounded_recovery", return_value=result),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/bounded/confirm",
                headers=_HDR,
                json={"project_id": "proj_alpha", "preparation_id": "prep_1"},
            )
        assert resp.status_code == 200
        assert resp.json()["operation_id"] == "op_1"


# ---------------------------------------------------------------------------
# 12.12 -- safe replace / append / rollback
# ---------------------------------------------------------------------------


class TestRollbackPreview:
    def test_missing_project_id_returns_400(self):
        with _auth_ok():
            client = _build_client()
            resp = client.get("/api/datastreams/ds_1/rollback/preview", headers=_HDR)
        assert resp.status_code == 400

    def test_role_denied_returns_404(self):
        _conn, fake_get = _fake_conn()
        with _auth_ok(), _role(False), patch("core.db.get_connection", new=fake_get):
            client = _build_client()
            resp = client.get(
                "/api/datastreams/ds_1/rollback/preview?project_id=proj_alpha",
                headers=_HDR,
            )
        assert resp.status_code == 404

    def test_happy_path_returns_200(self):
        _conn, fake_get = _fake_conn()
        preview = {"available": True, "target_execution_id": "dse_prior", "expired": False}
        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch("core.dataset_recovery.preview_rollback", return_value=preview),
        ):
            client = _build_client()
            resp = client.get(
                "/api/datastreams/ds_1/rollback/preview?project_id=proj_alpha",
                headers=_HDR,
            )
        assert resp.status_code == 200
        assert resp.json()["target_execution_id"] == "dse_prior"


class TestRollbackDataset:
    def _body(self, **over):
        body = {"project_id": "proj_alpha", "target_execution_id": "dse_prior"}
        body.update(over)
        return body

    def test_401_without_auth(self):
        with _auth_fail():
            client = _build_client()
            resp = client.post("/api/datastreams/ds_1/rollback", json=self._body())
        assert resp.status_code == 401

    def test_missing_target_returns_422(self):
        _conn, fake_get = _fake_conn()
        with _auth_ok(), _role(True), patch("core.db.get_connection", new=fake_get):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/rollback",
                headers=_HDR,
                json={"project_id": "proj_alpha"},
            )
        assert resp.status_code == 422

    def test_role_denied_returns_404(self):
        _conn, fake_get = _fake_conn()
        with _auth_ok(), _role(False), patch("core.db.get_connection", new=fake_get):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/rollback", headers=_HDR, json=self._body()
            )
        assert resp.status_code == 404

    def test_happy_path_returns_200(self):
        conn, fake_get = _fake_conn()
        result = {"rolled_back_from": "dse_cur", "rolled_back_to": "dse_prior"}
        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch("core.dataset_recovery.rollback_dataset", return_value=result),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/rollback", headers=_HDR, json=self._body()
            )
        assert resp.status_code == 200
        assert resp.json()["rolled_back_to"] == "dse_prior"
        conn.commit.assert_called()

    def test_window_expired_returns_409(self):
        _conn, fake_get = _fake_conn()
        from core.dataset_recovery import RollbackWindowExpired

        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch(
                "core.dataset_recovery.rollback_dataset",
                side_effect=RollbackWindowExpired("2026-01-01T00:00:00Z", "resolved_window"),
            ),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/rollback", headers=_HDR, json=self._body()
            )
        assert resp.status_code == 409
        assert resp.json()["code"] == "rollback_window_expired"

    def test_gate_failed_returns_422(self):
        _conn, fake_get = _fake_conn()
        from core.dataset_recovery import RollbackGateFailed

        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch(
                "core.dataset_recovery.rollback_dataset",
                side_effect=RollbackGateFailed([{"code": "empty_candidate"}]),
            ),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/rollback", headers=_HDR, json=self._body()
            )
        assert resp.status_code == 422
        assert resp.json()["issues"][0]["code"] == "empty_candidate"


class TestPreflightReplace:
    def _body(self, **over):
        body = {"project_id": "proj_alpha", "candidate_row_count": 5}
        body.update(over)
        return body

    def test_role_denied_returns_404(self):
        _conn, fake_get = _fake_conn()
        with _auth_ok(), _role(False), patch("core.db.get_connection", new=fake_get):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/replace/preflight", headers=_HDR, json=self._body()
            )
        assert resp.status_code == 404

    def test_happy_path_returns_200(self):
        _conn, fake_get = _fake_conn()
        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch(
                "core.dataset_recovery.preflight_replace",
                return_value={"ok": True, "action": "dataset.replace"},
            ),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/replace/preflight", headers=_HDR, json=self._body()
            )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_concurrent_mutation_returns_409(self):
        _conn, fake_get = _fake_conn()
        from core.dataset_recovery import ConcurrentMutationActive

        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch(
                "core.dataset_recovery.preflight_replace",
                side_effect=ConcurrentMutationActive("dse_active", "execution_in_flight"),
            ),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/replace/preflight", headers=_HDR, json=self._body()
            )
        assert resp.status_code == 409
        data = resp.json()
        assert data["code"] == "concurrent_mutation_active"
        assert data["blocking_execution_id"] == "dse_active"

    def test_empty_replace_blocked_returns_422(self):
        _conn, fake_get = _fake_conn()
        from core.dataset_recovery import EmptyReplacementBlocked

        with (
            _auth_ok(),
            _role(True),  # owner (force) allowed but the module still blocks the empty
            patch("core.db.get_connection", new=fake_get),
            patch(
                "core.dataset_recovery.preflight_replace",
                side_effect=EmptyReplacementBlocked(),
            ),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/replace/preflight",
                headers=_HDR,
                json=self._body(candidate_row_count=0, force_empty_publish=True),
            )
        assert resp.status_code == 422
        assert resp.json()["code"] == "empty_replacement_blocked"


class TestAppendAvailability:
    def test_role_denied_returns_404(self):
        _conn, fake_get = _fake_conn()
        with _auth_ok(), _role(False), patch("core.db.get_connection", new=fake_get):
            client = _build_client()
            resp = client.get(
                "/api/datastreams/ds_1/append/availability?project_id=proj_alpha",
                headers=_HDR,
            )
        assert resp.status_code == 404

    def test_happy_path_returns_200_with_fallback(self):
        _conn, fake_get = _fake_conn()
        avail = {
            "available": False,
            "fallback_action": "dataset.replace",
            "reason": "no_stable_key_contract",
        }
        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch("core.dataset_recovery.resolve_append_availability", return_value=avail),
        ):
            client = _build_client()
            resp = client.get(
                "/api/datastreams/ds_1/append/availability?project_id=proj_alpha",
                headers=_HDR,
            )
        assert resp.status_code == 200
        assert resp.json()["fallback_action"] == "dataset.replace"


class TestDestinationPolicy:
    def _op(self):
        from core.dataset_recovery import OWNER_FLOOR_OPERATIONS

        return sorted(OWNER_FLOOR_OPERATIONS)[0]

    def test_invalid_operation_returns_422(self):
        with _auth_ok():
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/destination-policy",
                headers=_HDR,
                json={"project_id": "proj_alpha", "operation": "not_a_policy_op"},
            )
        assert resp.status_code == 422
        assert resp.json()["code"] == "invalid_operation"

    def test_owner_floor_required_returns_403(self):
        _conn, fake_get = _fake_conn()
        from core.dataset_recovery import OwnerFloorRequired

        with (
            _auth_ok(),
            _role(True),  # passes the viewer scope gate; owner floor still enforced
            patch("core.db.get_connection", new=fake_get),
            patch(
                "core.dataset_recovery.enforce_owner_floor",
                side_effect=OwnerFloorRequired(self._op()),
            ),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/destination-policy",
                headers=_HDR,
                json={"project_id": "proj_alpha", "operation": self._op()},
            )
        assert resp.status_code == 403
        assert resp.json()["code"] == "owner_floor_required"

    def test_happy_path_returns_200(self):
        _conn, fake_get = _fake_conn()
        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch("core.dataset_recovery.enforce_owner_floor", return_value=None),
        ):
            client = _build_client()
            resp = client.post(
                "/api/datastreams/ds_1/destination-policy",
                headers=_HDR,
                json={"project_id": "proj_alpha", "operation": self._op()},
            )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# 12.14 -- versioned Datastream read model
# ---------------------------------------------------------------------------


class TestDatastreamReadModel:
    def test_missing_project_id_returns_400(self):
        with _auth_ok():
            client = _build_client()
            resp = client.get("/api/datastreams/ds_1/read-model", headers=_HDR)
        assert resp.status_code == 400

    def test_role_denied_returns_404(self):
        _conn, fake_get = _fake_conn()
        with _auth_ok(), _role(False), patch("core.db.get_connection", new=fake_get):
            client = _build_client()
            resp = client.get(
                "/api/datastreams/ds_1/read-model?project_id=proj_alpha", headers=_HDR
            )
        assert resp.status_code == 404

    def test_unknown_datastream_returns_404(self):
        _conn, fake_get = _fake_conn()
        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch("core.datastreams.get_datastream", return_value=None),
        ):
            client = _build_client()
            resp = client.get(
                "/api/datastreams/ds_1/read-model?project_id=proj_alpha", headers=_HDR
            )
        assert resp.status_code == 404

    def test_happy_path_assembles_read_model(self):
        _conn, fake_get = _fake_conn()
        with (
            _auth_ok(),
            _role(True),
            patch("core.db.get_connection", new=fake_get),
            patch("core.datastreams.get_datastream", return_value={"id": "ds_1"}),
            patch(
                "core.datastream_intents.list_intent_versions",
                return_value=[{"id": "dsp_1", "version_number": 1}],
            ),
            patch(
                "core.datastream_field_mapping.list_mapping_versions",
                return_value=[{"id": "dmap_1"}],
            ),
            patch(
                "core.admin_api._read_current_published_execution",
                return_value="dse_pub",
            ),
            patch(
                "core.datastream_publication.get_execution",
                return_value={"id": "dse_pub", "state": "published", "row_count": 42},
            ),
            patch(
                "core.admin_api._read_current_candidate_execution",
                return_value={"id": "dse_cand", "state": "validating"},
            ),
            patch(
                "core.datastream_publication.get_publication_log",
                return_value=[{"id": "dplog_1", "published_by": "u1", "prior_execution_id": None}],
            ),
            patch("core.managed_feed_ledger.list_ledger", return_value=[{"id": "mfl_1"}]),
        ):
            client = _build_client()
            resp = client.get(
                "/api/datastreams/ds_1/read-model?project_id=proj_alpha", headers=_HDR
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["plan_versions"][0]["id"] == "dsp_1"
        assert data["mapping_versions"][0]["id"] == "dmap_1"
        assert data["current_published_execution_id"] == "dse_pub"
        assert data["published_execution"]["row_count"] == 42
        assert data["current_candidate"]["state"] == "validating"
        assert data["publication_log"][0]["published_by"] == "u1"
        assert data["recent_imports"][0]["id"] == "mfl_1"
