"""Offline unit tests for Story 24.2 -- org warehouse schema provisioning.

No real DB and no real DuckDB file are required: all external calls are mocked.
Covers AC1-AC6 (provision, drop, backfill, delete endpoint, BigQuery gate).
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AUTH = ("core.admin_api._check_auth", (True, "admin@example.com"))


def _delete_request(org_id: str, *, confirm: bool = False) -> MagicMock:
    req = MagicMock()
    req.path_params = {"org_id": org_id}
    req.body = AsyncMock(return_value=b"")
    req.headers = {"X-Confirm-Delete": "drop-warehouse-data"} if confirm else {}
    return req


def _post_request(org_id: str, body: dict | None = None) -> MagicMock:
    req = MagicMock()
    req.path_params = {"org_id": org_id}
    req.body = AsyncMock(return_value=json.dumps(body or {}).encode())
    req.headers = {}
    return req


def _backfill_request(body: dict | None = None) -> MagicMock:
    req = MagicMock()
    req.path_params = {}
    _body = json.dumps(body or {}).encode()
    # body() may be called with await; also must be truthy for the parse branch.
    req.body = AsyncMock(return_value=_body)
    req.headers = {}
    return req


def _make_pg_conn(rows=None):
    """Return a mock psycopg connection that returns *rows* from fetchall/fetchone."""
    cur = MagicMock()
    cur.fetchone.return_value = rows[0] if rows else None
    cur.fetchall.return_value = rows or []
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cm = MagicMock()
    cm.__enter__.return_value = conn
    cm.__exit__.return_value = False
    return cm, cur, conn


# ---------------------------------------------------------------------------
# provision_org_schemas -- warehouse_tenancy level
# ---------------------------------------------------------------------------


def test_provision_skips_when_no_duckdb_path(monkeypatch, caplog):
    """AC1: missing TOOROW_DUCKDB_PATH -> skipped + WARNING, no exception."""
    import logging

    monkeypatch.delenv("TOOROW_DUCKDB_PATH", raising=False)
    monkeypatch.delenv("TOOROW_DB_MODE", raising=False)
    from core import warehouse_tenancy as wt

    wt._reset_cache()

    # Resolve returns a valid OrgSchemas via a mocked Postgres lookup.
    slug_row = ("org_01", "acme-corp")
    with patch("core.db.get_connection") as mock_gc:
        cur = MagicMock()
        cur.fetchone.return_value = slug_row
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cm = MagicMock()
        cm.__enter__.return_value = conn
        cm.__exit__.return_value = False
        mock_gc.return_value = cm

        with caplog.at_level(logging.WARNING, logger="core.warehouse_tenancy"):
            result = wt.provision_org_schemas(org_id="org_01")

    assert result["status"] == "skipped"
    assert result["reason"] == "no_duckdb_path"
    assert any("TOOROW_DUCKDB_PATH" in r.message for r in caplog.records)
    wt._reset_cache()


def test_provision_calls_create_schema_twice(monkeypatch):
    """AC1 + AC2: with a valid DuckDB path, CREATE SCHEMA IF NOT EXISTS called x2."""
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", "/tmp/test.duckdb")
    monkeypatch.delenv("TOOROW_DB_MODE", raising=False)
    from core import warehouse_tenancy as wt

    wt._reset_cache()

    slug_row = ("org_01", "acme-corp")
    mock_duck_conn = MagicMock()

    with (
        patch("core.db.get_connection") as mock_gc,
        patch("duckdb.connect", return_value=mock_duck_conn),
    ):
        cur = MagicMock()
        cur.fetchone.return_value = slug_row
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cm = MagicMock()
        cm.__enter__.return_value = conn
        cm.__exit__.return_value = False
        mock_gc.return_value = cm

        result = wt.provision_org_schemas(org_id="org_01")

    assert result["status"] == "ok"
    assert result["raw"] == "org_acme_corp_raw"
    assert result["marts"] == "org_acme_corp_marts"
    calls = mock_duck_conn.execute.call_args_list
    assert any("CREATE SCHEMA IF NOT EXISTS org_acme_corp_raw" in str(c) for c in calls)
    assert any("CREATE SCHEMA IF NOT EXISTS org_acme_corp_marts" in str(c) for c in calls)
    # Ensure raw before marts (AC2 order)
    raw_idx = next(i for i, c in enumerate(calls) if "org_acme_corp_raw" in str(c))
    marts_idx = next(i for i, c in enumerate(calls) if "org_acme_corp_marts" in str(c))
    assert raw_idx < marts_idx
    wt._reset_cache()


def test_provision_skips_unresolvable_slug(monkeypatch, caplog):
    """AC3 (backfill): org with unresolvable slug (None from DB) -> skipped."""
    import logging

    monkeypatch.setenv("TOOROW_DUCKDB_PATH", "/tmp/test.duckdb")
    monkeypatch.delenv("TOOROW_DB_MODE", raising=False)
    from core import warehouse_tenancy as wt

    wt._reset_cache()

    with patch("core.db.get_connection") as mock_gc:
        cur = MagicMock()
        cur.fetchone.return_value = None  # org not found -> schemas = None
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cm = MagicMock()
        cm.__enter__.return_value = conn
        cm.__exit__.return_value = False
        mock_gc.return_value = cm

        with caplog.at_level(logging.WARNING, logger="core.warehouse_tenancy"):
            result = wt.provision_org_schemas(org_id="org_bad")

    assert result["status"] == "skipped"
    assert result["reason"] == "unresolvable"
    wt._reset_cache()


# ---------------------------------------------------------------------------
# drop_org_schemas -- warehouse_tenancy level
# ---------------------------------------------------------------------------


def test_drop_calls_drop_schema_cascade_twice(monkeypatch):
    """AC5: DROP SCHEMA IF EXISTS ... CASCADE called for raw and marts."""
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", "/tmp/test.duckdb")
    monkeypatch.delenv("TOOROW_DB_MODE", raising=False)
    from core import warehouse_tenancy as wt

    wt._reset_cache()

    slug_row = ("org_01", "brand-x")
    mock_duck_conn = MagicMock()

    with (
        patch("core.db.get_connection") as mock_gc,
        patch("duckdb.connect", return_value=mock_duck_conn),
    ):
        cur = MagicMock()
        cur.fetchone.return_value = slug_row
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cm = MagicMock()
        cm.__enter__.return_value = conn
        cm.__exit__.return_value = False
        mock_gc.return_value = cm

        result = wt.drop_org_schemas(org_id="org_01")

    assert result["status"] == "ok"
    calls = mock_duck_conn.execute.call_args_list
    assert any("DROP SCHEMA IF EXISTS org_brand_x_raw CASCADE" in str(c) for c in calls)
    assert any("DROP SCHEMA IF EXISTS org_brand_x_marts CASCADE" in str(c) for c in calls)
    wt._reset_cache()


def test_drop_skips_when_no_duckdb_path(monkeypatch):
    """AC5: drop with no DuckDB path -> skipped (nothing to drop)."""
    monkeypatch.delenv("TOOROW_DUCKDB_PATH", raising=False)
    monkeypatch.delenv("TOOROW_DB_MODE", raising=False)
    from core import warehouse_tenancy as wt

    wt._reset_cache()

    slug_row = ("org_01", "brand-x")
    with patch("core.db.get_connection") as mock_gc:
        cur = MagicMock()
        cur.fetchone.return_value = slug_row
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cm = MagicMock()
        cm.__enter__.return_value = conn
        cm.__exit__.return_value = False
        mock_gc.return_value = cm

        result = wt.drop_org_schemas(org_id="org_01")

    assert result["status"] == "skipped"
    assert result["reason"] == "no_duckdb_path"
    wt._reset_cache()


# ---------------------------------------------------------------------------
# BigQuery gate (AC6)
# ---------------------------------------------------------------------------


def test_bigquery_provision_gated_by_default(monkeypatch):
    """AC6: TOOROW_DB_MODE=bigquery without flag -> skipped, _provision_bigquery_datasets
    never called (gate fires before any BQ code path).

    Proof: mock _provision_bigquery_datasets and assert_not_called -- this is the real
    evidence that the gate works, regardless of what is in sys.modules (F-3).
    """
    monkeypatch.setenv("TOOROW_DB_MODE", "bigquery")
    monkeypatch.delenv("TOOROW_BQ_PROVISION_ENABLED", raising=False)
    from core import warehouse_tenancy as wt

    wt._reset_cache()

    slug_row = ("org_01", "acme-corp")
    with (
        patch("core.db.get_connection") as mock_gc,
        patch("core.warehouse_tenancy._provision_bigquery_datasets") as mock_bq,
    ):
        cur = MagicMock()
        cur.fetchone.return_value = slug_row
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cm = MagicMock()
        cm.__enter__.return_value = conn
        cm.__exit__.return_value = False
        mock_gc.return_value = cm

        result = wt.provision_org_schemas(org_id="org_01")

    assert result["status"] == "skipped"
    assert result["reason"] == "bq_gated_ai08"
    # Primary proof: the BQ provisioning function was never reached.
    mock_bq.assert_not_called()
    wt._reset_cache()


def _mock_org_connection(slug_row=("org_01", "acme-corp")):
    cur = MagicMock()
    cur.fetchone.return_value = slug_row
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cm = MagicMock()
    cm.__enter__.return_value = conn
    cm.__exit__.return_value = False
    return cm


def test_bigquery_provisioning_does_not_require_bigquery_landing_mode(monkeypatch):
    """The org's warehouse and the pull landing target are separate decisions.

    While the two were tied to TOOROW_DB_MODE, provisioning could only be turned
    on by flipping the platform to a landing mode that eight connectors refuse --
    so "create an org, get its datasets" was unreachable in practice.
    """
    monkeypatch.setenv("TOOROW_BQ_PROVISION_ENABLED", "1")
    monkeypatch.delenv("TOOROW_DB_MODE", raising=False)
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", "/should/not/be/touched.db")
    from core import warehouse_tenancy as wt

    wt._reset_cache()
    with (
        patch("core.db.get_connection") as mock_gc,
        patch("core.warehouse_tenancy._provision_bigquery_datasets") as mock_bq,
    ):
        mock_gc.return_value = _mock_org_connection()
        result = wt.provision_org_schemas(org_id="org_01")

    assert result["status"] == "ok"
    assert result["raw"] == "org_acme_corp_raw"
    assert result["marts"] == "org_acme_corp_marts"
    mock_bq.assert_called_once()
    wt._reset_cache()


def test_bigquery_drop_is_symmetric_with_provisioning(monkeypatch):
    """Whatever creation provisioned, the RGPD drop must remove.

    A drop that fell through to the DuckDB branch would report success without
    deleting the dataset -- the endpoint would then delete the Postgres row while
    the tenant's data survived in BigQuery.
    """
    monkeypatch.setenv("TOOROW_BQ_PROVISION_ENABLED", "1")
    monkeypatch.delenv("TOOROW_DB_MODE", raising=False)
    from core import warehouse_tenancy as wt

    wt._reset_cache()
    with (
        patch("core.db.get_connection") as mock_gc,
        patch("core.warehouse_tenancy._drop_bigquery_datasets") as mock_drop,
    ):
        mock_gc.return_value = _mock_org_connection()
        result = wt.drop_org_schemas(org_id="org_01")

    assert result["status"] == "ok"
    mock_drop.assert_called_once()
    wt._reset_cache()


def test_duckdb_remains_the_default_when_the_flag_is_absent(monkeypatch):
    """No flag, no TOOROW_DB_MODE -> unchanged DuckDB behaviour, no BQ call."""
    monkeypatch.delenv("TOOROW_BQ_PROVISION_ENABLED", raising=False)
    monkeypatch.delenv("TOOROW_DB_MODE", raising=False)
    monkeypatch.delenv("TOOROW_DUCKDB_PATH", raising=False)
    from core import warehouse_tenancy as wt

    wt._reset_cache()
    with (
        patch("core.db.get_connection") as mock_gc,
        patch("core.warehouse_tenancy._provision_bigquery_datasets") as mock_bq,
    ):
        mock_gc.return_value = _mock_org_connection()
        result = wt.provision_org_schemas(org_id="org_01")

    assert result["status"] == "skipped"
    assert result["reason"] == "no_duckdb_path"
    mock_bq.assert_not_called()
    wt._reset_cache()


# ---------------------------------------------------------------------------
# _delete_org endpoint -- admin_api level
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_delete_org_requires_confirmation():
    """AC5: DELETE without X-Confirm-Delete header -> 422 confirmation_required."""
    from core.admin_api import _delete_org

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _delete_org(_delete_request("org_123", confirm=False))
    assert resp.status_code == 422
    body = json.loads(resp.body)
    assert body["code"] == "confirmation_required"


@pytest.mark.anyio
async def test_delete_org_blocks_when_active_projects():
    """AC5: active projects present -> 409 org_has_active_projects.

    New transactional flow (F-2): a single connection is kept open; get_connection()
    is called once and pg_conn = cm.__enter__() is used directly.
    """
    from core.admin_api import _delete_org

    # Simulate: org exists, no manage-denial, active project found.
    org_row = ("org_01", "Test Org", "test-org")

    call_count = [0]

    def side_effect_fetchone():
        call_count[0] += 1
        if call_count[0] == 1:
            return org_row  # org exists
        return ("proj_01",)  # active project exists

    cur = MagicMock()
    cur.fetchone.side_effect = side_effect_fetchone
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cm = MagicMock()
    cm.__enter__.return_value = conn
    cm.__exit__.return_value = False

    with (
        patch(_AUTH[0], return_value=_AUTH[1]),
        patch("core.db.get_connection", return_value=cm),
        patch("core.admin_api._enforce_org_manage", return_value=None),
    ):
        resp = await _delete_org(_delete_request("org_01", confirm=True))

    assert resp.status_code == 409
    body = json.loads(resp.body)
    assert body["code"] == "org_has_active_projects"


@pytest.mark.anyio
async def test_delete_org_full_flow_emits_audit_in_order():
    """AC5 (F-2/F-7): successful delete emits org_schemas_dropped THEN org_deleted,
    both via insert_audit_row on the same open transaction (not write_audit_row).
    """
    from core.admin_api import _delete_org

    org_row = ("org_01", "Test Org", "test-org")

    call_count = [0]

    def fetchone_side():
        call_count[0] += 1
        if call_count[0] == 1:
            return org_row  # org found
        return None  # no active projects

    cur = MagicMock()
    cur.fetchone.side_effect = fetchone_side
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cm = MagicMock()
    cm.__enter__.return_value = conn
    cm.__exit__.return_value = False

    audit_calls: list[str] = []

    def fake_insert_audit(pg_conn, *, identity, action, **kwargs):
        audit_calls.append(action)

    with (
        patch(_AUTH[0], return_value=_AUTH[1]),
        patch("core.db.get_connection", return_value=cm),
        patch("core.admin_api._enforce_org_manage", return_value=None),
        patch(
            "core.warehouse_tenancy.drop_org_schemas",
            return_value={"status": "ok", "raw": "org_test_org_raw", "marts": "org_test_org_marts"},
        ),
        patch("core.audit.insert_audit_row", side_effect=fake_insert_audit),
    ):
        resp = await _delete_org(_delete_request("org_01", confirm=True))

    assert resp.status_code == 200
    # Audit order (F-7): schemas_dropped (because status="ok") before org_deleted.
    assert audit_calls[0] == "org_schemas_dropped"
    assert audit_calls[1] == "org_deleted"


@pytest.mark.anyio
async def test_delete_org_skipped_drop_emits_only_org_deleted():
    """F-7: when drop is skipped (no_duckdb_path), only org_deleted is emitted."""
    from core.admin_api import _delete_org

    org_row = ("org_01", "Test Org", "test-org")

    call_count = [0]

    def fetchone_side():
        call_count[0] += 1
        if call_count[0] == 1:
            return org_row
        return None  # no active projects

    cur = MagicMock()
    cur.fetchone.side_effect = fetchone_side
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cm = MagicMock()
    cm.__enter__.return_value = conn
    cm.__exit__.return_value = False

    audit_calls: list[str] = []

    def fake_insert_audit(pg_conn, *, identity, action, **kwargs):
        audit_calls.append(action)

    with (
        patch(_AUTH[0], return_value=_AUTH[1]),
        patch("core.db.get_connection", return_value=cm),
        patch("core.admin_api._enforce_org_manage", return_value=None),
        patch(
            "core.warehouse_tenancy.drop_org_schemas",
            return_value={"status": "skipped", "reason": "no_duckdb_path"},
        ),
        patch("core.audit.insert_audit_row", side_effect=fake_insert_audit),
    ):
        resp = await _delete_org(_delete_request("org_01", confirm=True))

    assert resp.status_code == 200
    # Only org_deleted, NOT org_schemas_dropped (nothing was dropped).
    assert audit_calls == ["org_deleted"]


@pytest.mark.anyio
async def test_delete_org_blocks_when_drop_raises():
    """AC5 (RGPD): if drop raises, return 500 and do NOT commit Postgres DELETE.

    F-2: in the new transactional flow, pg_conn is rolled back when drop raises,
    so the DELETE (already issued) is undone -- org row stays intact.
    """
    from core.admin_api import _delete_org

    org_row = ("org_01", "Test Org", "test-org")
    call_count = [0]

    def fetchone_side():
        call_count[0] += 1
        if call_count[0] == 1:
            return org_row
        return None

    cur = MagicMock()
    cur.fetchone.side_effect = fetchone_side
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cm = MagicMock()
    cm.__enter__.return_value = conn
    cm.__exit__.return_value = False

    with (
        patch(_AUTH[0], return_value=_AUTH[1]),
        patch("core.db.get_connection", return_value=cm),
        patch("core.admin_api._enforce_org_manage", return_value=None),
        patch(
            "core.warehouse_tenancy.drop_org_schemas",
            side_effect=RuntimeError("IO error"),
        ),
    ):
        resp = await _delete_org(_delete_request("org_01", confirm=True))

    assert resp.status_code == 500
    body = json.loads(resp.body)
    assert body["code"] == "schema_drop_failed"
    # The connection must have been rolled back (not committed).
    conn.rollback.assert_called()
    conn.commit.assert_not_called()


# ---------------------------------------------------------------------------
# _backfill_warehouse_schemas endpoint
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_backfill_calls_provision_for_each_org():
    """AC3: backfill calls provision_org_schemas once per org returned by DB."""
    from core.admin_api import _backfill_warehouse_schemas

    org_rows = [("org_01",), ("org_02",), ("org_03",)]

    cur = MagicMock()
    cur.fetchall.return_value = org_rows
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cm = MagicMock()
    cm.__enter__.return_value = conn
    cm.__exit__.return_value = False

    provision_calls: list[str] = []

    def fake_provision(org_id, conn=None):
        provision_calls.append(org_id)
        return {"status": "ok", "raw": f"r_{org_id}", "marts": f"m_{org_id}"}

    with (
        patch(_AUTH[0], return_value=_AUTH[1]),
        patch("core.db.get_connection", return_value=cm),
        patch("core.warehouse_tenancy.provision_org_schemas", side_effect=fake_provision),
    ):
        resp = await _backfill_warehouse_schemas(_backfill_request())

    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["provisioned"] == 3
    assert body["skipped"] == 0
    assert body["errors"] == []
    assert set(provision_calls) == {"org_01", "org_02", "org_03"}


@pytest.mark.anyio
async def test_backfill_counts_skipped_and_errors():
    """AC3: mix of ok/skipped/error correctly reflected in response."""
    from core.admin_api import _backfill_warehouse_schemas

    org_rows = [("org_01",), ("org_02",), ("org_03",)]

    cur = MagicMock()
    cur.fetchall.return_value = org_rows
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cm = MagicMock()
    cm.__enter__.return_value = conn
    cm.__exit__.return_value = False

    call_n = [0]

    def mixed_provision(org_id, conn=None):
        call_n[0] += 1
        if call_n[0] == 1:
            return {"status": "ok", "raw": "r", "marts": "m"}
        if call_n[0] == 2:
            return {"status": "skipped", "reason": "no_duckdb_path"}
        raise RuntimeError("boom")

    with (
        patch(_AUTH[0], return_value=_AUTH[1]),
        patch("core.db.get_connection", return_value=cm),
        patch("core.warehouse_tenancy.provision_org_schemas", side_effect=mixed_provision),
    ):
        resp = await _backfill_warehouse_schemas(_backfill_request())

    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["provisioned"] == 1
    assert body["skipped"] == 1
    assert len(body["errors"]) == 1
    assert body["errors"][0]["org_id"] == "org_03"


# ---------------------------------------------------------------------------
# _provision_org_warehouse endpoint
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_provision_org_warehouse_404_when_not_found():
    """AC4: org not found -> 404."""
    from core.admin_api import _provision_org_warehouse

    cur = MagicMock()
    cur.fetchone.return_value = None
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cm = MagicMock()
    cm.__enter__.return_value = conn
    cm.__exit__.return_value = False

    with (
        patch(_AUTH[0], return_value=_AUTH[1]),
        patch("core.db.get_connection", return_value=cm),
    ):
        resp = await _provision_org_warehouse(_post_request("org_missing"))

    assert resp.status_code == 404


@pytest.mark.anyio
async def test_provision_org_warehouse_success_emits_audit():
    """AC4: successful manual provision emits org_schemas_provisioned."""
    from core.admin_api import _provision_org_warehouse

    cur = MagicMock()
    cur.fetchone.return_value = ("org_01",)  # org exists
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cm = MagicMock()
    cm.__enter__.return_value = conn
    cm.__exit__.return_value = False

    audit_calls: list[str] = []

    with (
        patch(_AUTH[0], return_value=_AUTH[1]),
        patch("core.db.get_connection", return_value=cm),
        patch("core.admin_api._enforce_org_manage", return_value=None),
        patch(
            "core.warehouse_tenancy.provision_org_schemas",
            return_value={"status": "ok", "raw": "org_x_raw", "marts": "org_x_marts"},
        ),
        patch(
            "core.admin_api.write_audit_row",
            side_effect=lambda **kw: audit_calls.append(kw["action"]),
        ),
    ):
        resp = await _provision_org_warehouse(_post_request("org_01"))

    assert resp.status_code == 200
    assert "org_schemas_provisioned" in audit_calls
