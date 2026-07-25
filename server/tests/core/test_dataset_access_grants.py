"""Tests for Story 24.5 -- dataset marts access grants (Epic 24, P4).

Offline: input validation (handlers reject before touching the DB) + BigQuery
IAM stub invariants (AC6, AC7) -- all runnable without a Postgres connection.

Live-Postgres (skipped when TEST_POSTGRES_DSN is unset): structural isolation --
a grant is uniquely scoped to (org_id, principal) WHERE revoked_at IS NULL, so
duplicate active grants are prevented while re-granting after revocation is
allowed (new row, RGPD trace preserved).  Calqué sur test_credential_grants.py.
"""

from __future__ import annotations

import json
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


# ---------------------------------------------------------------------------
# Postgres availability check (calqué sur test_credential_grants.py)
# ---------------------------------------------------------------------------


def _pg_reachable() -> bool:
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False
    try:
        import psycopg

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


pg_available = pytest.mark.skipif(not _pg_reachable(), reason="platform Postgres not reachable")

_AUTH = ("core.admin_api._check_auth", (True, "tester@example.com"))

# A valid IAM principal (serviceAccount format)
_VALID_PRINCIPAL = "serviceAccount:sa@project.iam.gserviceaccount.com"
_VALID_PRINCIPAL_2 = "user:alice@example.com"


# ---------------------------------------------------------------------------
# Request builders (calqués sur test_credential_grants.py)
# ---------------------------------------------------------------------------


def _post(org_id: str, body: dict) -> MagicMock:
    req = MagicMock()
    req.path_params = {"org_id": org_id}
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    return req


def _get(org_id: str, **path) -> MagicMock:
    req = MagicMock()
    req.path_params = {"org_id": org_id, **path}
    return req


def _delete(org_id: str, grant_id: str) -> MagicMock:
    req = MagicMock()
    req.path_params = {"org_id": org_id, "grant_id": grant_id}
    return req


# ---------------------------------------------------------------------------
# Live fixture helpers (direct SQL; avoids the heavy create-project flow)
# ---------------------------------------------------------------------------


def _setup_org(suffix: str) -> dict:
    """Create a minimal org for dataset access grant tests.

    Returns the ids dict.  Teardown via _teardown_org.
    """
    from core.db import get_connection

    org_id = f"dag_org_{suffix}"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, created_by) "
                "VALUES (%s, %s, %s, 'system')",
                (org_id, f"DAGOrg-{suffix}", f"dag-org-{suffix}"),
            )
        conn.commit()
    return {"org_id": org_id}


def _teardown_org(ids: dict) -> None:
    from core.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            # ON DELETE CASCADE removes dataset_access_grants automatically.
            cur.execute(
                "DELETE FROM app.organizations WHERE id = %s", (ids["org_id"],)
            )
        conn.commit()


# ---------------------------------------------------------------------------
# Offline validation -- no DB, no auth
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_grant_requires_principal_422():
    """POST without principal → 422 invalid_input (AC2)."""
    from core.admin_api import _grant_dataset_access

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _grant_dataset_access(_post("org_x", {}))
    assert resp.status_code == 422
    body = json.loads(resp.body)
    assert body["code"] == "invalid_input"


@pytest.mark.anyio
async def test_grant_requires_valid_principal_format_422():
    """POST with a principal missing '@' → 422 (AC2 IAM format validation)."""
    from core.admin_api import _grant_dataset_access

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _grant_dataset_access(
            _post("org_x", {"principal": "not-a-valid-principal"})
        )
    assert resp.status_code == 422
    body = json.loads(resp.body)
    assert body["code"] == "invalid_input"


@pytest.mark.anyio
async def test_grant_requires_valid_iam_type_422():
    """POST with unknown IAM type → 422 (AC2 type ∈ {user, serviceAccount, group})."""
    from core.admin_api import _grant_dataset_access

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _grant_dataset_access(
            _post("org_x", {"principal": "role:admin@example.com"})
        )
    assert resp.status_code == 422
    body = json.loads(resp.body)
    assert body["code"] == "invalid_input"


@pytest.mark.anyio
async def test_grant_requires_auth_401():
    """POST without auth → 401 (AC2)."""
    from core.admin_api import _grant_dataset_access

    with patch(_AUTH[0], return_value=(False, "")):
        resp = await _grant_dataset_access(_post("org_x", {"principal": _VALID_PRINCIPAL}))
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_list_requires_auth_401():
    """GET without auth → 401 (AC4)."""
    from core.admin_api import _list_dataset_access_grants

    with patch(_AUTH[0], return_value=(False, "")):
        resp = await _list_dataset_access_grants(_get("org_x"))
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_revoke_requires_auth_401():
    """DELETE without auth → 401 (AC5)."""
    from core.admin_api import _revoke_dataset_access

    with patch(_AUTH[0], return_value=(False, "")):
        resp = await _revoke_dataset_access(_delete("org_x", "dagrant_123"))
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Offline -- BigQuery IAM stub invariants (AC6, AC7)
# ---------------------------------------------------------------------------


def test_simulate_bq_grant_rejects_raw_dataset():
    """_simulate_bq_iam_grant raises ValueError if marts contains '_raw' (AC7).

    ValueError, not assert: the guard must survive `python -O` (review F-6)."""
    from core.warehouse_tenancy import OrgSchemas, _simulate_bq_iam_grant

    bad_schemas = OrgSchemas(
        org_id="org_x",
        org_slug="x",
        warehouse_slug="x",
        raw="org_x_raw",
        marts="org_x_raw",  # intentionally wrong: same as raw
    )
    with pytest.raises(ValueError, match="_raw"):
        _simulate_bq_iam_grant(bad_schemas, _VALID_PRINCIPAL, "grant")


def test_simulate_bq_grant_rejects_mirror_dataset():
    """_simulate_bq_iam_grant raises ValueError if marts contains 'mirror_' (AC7)."""
    from core.warehouse_tenancy import OrgSchemas, _simulate_bq_iam_grant

    bad_schemas = OrgSchemas(
        org_id="org_x",
        org_slug="x",
        warehouse_slug="x",
        raw="org_x_raw",
        marts="mirror_org_x_marts",  # intentionally wrong
    )
    with pytest.raises(ValueError, match="mirror_"):
        _simulate_bq_iam_grant(bad_schemas, _VALID_PRINCIPAL, "grant")


def test_simulate_bq_grant_valid_marts_logs_info(caplog):
    """_simulate_bq_iam_grant logs INFO with action/dataset/principal (AC6)."""
    import logging

    from core.warehouse_tenancy import OrgSchemas, _simulate_bq_iam_grant

    schemas = OrgSchemas(
        org_id="org_demo",
        org_slug="demo",
        warehouse_slug="demo",
        raw="org_demo_raw",
        marts="org_demo_marts",
    )
    with caplog.at_level(logging.INFO, logger="core.warehouse_tenancy"):
        _simulate_bq_iam_grant(schemas, _VALID_PRINCIPAL, "grant")

    assert any("bq_iam_simulate" in r.message for r in caplog.records)
    assert any("org_demo_marts" in r.message for r in caplog.records)
    assert any("grant" in r.message for r in caplog.records)


@pytest.mark.anyio
async def test_bq_simulate_not_called_flag_off():
    """When org_schemas_enabled()=False, _simulate_bq_iam_grant is never called (AC6 flag OFF)."""
    from unittest.mock import MagicMock, patch

    from core.admin_api import _grant_dataset_access

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.fetchone.side_effect = [
        None,   # no existing active grant
        (      # RETURNING row after INSERT
            "dagrant_test",
            "org_flag_off",
            _VALID_PRINCIPAL,
            "tester@example.com",
            None,
        ),
    ]
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor = MagicMock(return_value=mock_cursor)

    with (
        patch(_AUTH[0], return_value=_AUTH[1]),
        patch("core.admin_api._enforce_org_manage", return_value=None),
        patch("core.db.get_connection", return_value=mock_conn),
        patch("core.warehouse_tenancy.org_schemas_enabled", return_value=False),
        patch("core.warehouse_tenancy._simulate_bq_iam_grant") as mock_sim,
        patch("core.admin_api.write_audit_row") as mock_audit,
    ):
        resp = await _grant_dataset_access(
            _post("org_flag_off", {"principal": _VALID_PRINCIPAL})
        )

    # The stub MUST NOT be called when the flag is OFF (AC6).
    mock_sim.assert_not_called()
    assert resp.status_code == 201
    # Audit IS emitted (the Postgres grant row exists) with an honest marker
    # that no BQ binding was simulated (review 24.5 F-3/F-4).
    mock_audit.assert_called_once()
    meta = mock_audit.call_args.kwargs["metadata"]
    assert meta["bq_binding"] == "flag_off_not_simulated"
    assert meta["marts_dataset"] is None


@pytest.mark.anyio
async def test_bq_simulate_called_flag_on():
    """Flag ON: _simulate_bq_iam_grant is called with action='grant' (AC6)."""
    from core.admin_api import _grant_dataset_access
    from core.warehouse_tenancy import OrgSchemas

    fake_schemas = OrgSchemas(
        org_id="org_flag_on",
        org_slug="flag-on",
        warehouse_slug="flag_on",
        raw="org_flag_on_raw",
        marts="org_flag_on_marts",
    )

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.fetchone.side_effect = [
        None,  # no existing active grant
        (
            "dagrant_flagtest",
            "org_flag_on",
            _VALID_PRINCIPAL,
            "tester@example.com",
            None,
        ),
    ]
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor = MagicMock(return_value=mock_cursor)

    with (
        patch(_AUTH[0], return_value=_AUTH[1]),
        patch("core.admin_api._enforce_org_manage", return_value=None),
        patch("core.db.get_connection", return_value=mock_conn),
        patch("core.warehouse_tenancy.org_schemas_enabled", return_value=True),
        patch("core.warehouse_tenancy.resolve_org_schemas", return_value=fake_schemas),
        patch("core.warehouse_tenancy._simulate_bq_iam_grant") as mock_sim,
        patch("core.admin_api.write_audit_row") as mock_audit,
    ):
        resp = await _grant_dataset_access(
            _post("org_flag_on", {"principal": _VALID_PRINCIPAL})
        )

    assert resp.status_code == 201
    mock_audit.assert_called_once()
    assert mock_audit.call_args.kwargs["metadata"]["bq_binding"] == "simulated"
    mock_sim.assert_called_once_with(fake_schemas, _VALID_PRINCIPAL, "grant")
    # Verify dataset passed is marts and never contains _raw or mirror_.
    called_schemas = mock_sim.call_args[0][0]
    assert "_raw" not in called_schemas.marts
    assert "mirror_" not in called_schemas.marts
    assert called_schemas.marts.endswith("_marts")


# ---------------------------------------------------------------------------
# Live Postgres -- structural isolation
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_grant_and_list_active_grants():
    """POST creates the row; GET returns it; revoked rows are excluded (AC2, AC4)."""
    from core.admin_api import _grant_dataset_access, _list_dataset_access_grants

    suffix = uuid.uuid4().hex[:8]
    ids = _setup_org(suffix)
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            r = await _grant_dataset_access(
                _post(ids["org_id"], {"principal": _VALID_PRINCIPAL})
            )
            assert r.status_code == 201
            data = json.loads(r.body)
            assert data["principal"] == _VALID_PRINCIPAL
            assert data["org_id"] == ids["org_id"]

            grants_resp = await _list_dataset_access_grants(_get(ids["org_id"]))
            assert grants_resp.status_code == 200
            grants = json.loads(grants_resp.body)["grants"]
            assert len(grants) == 1
            assert grants[0]["principal"] == _VALID_PRINCIPAL
    finally:
        _teardown_org(ids)


@pg_available
@pytest.mark.anyio
async def test_duplicate_grant_409():
    """Double POST with same principal → 409 conflict (AC3)."""
    from core.admin_api import _grant_dataset_access

    suffix = uuid.uuid4().hex[:8]
    ids = _setup_org(suffix)
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            r1 = await _grant_dataset_access(
                _post(ids["org_id"], {"principal": _VALID_PRINCIPAL})
            )
            assert r1.status_code == 201
            r2 = await _grant_dataset_access(
                _post(ids["org_id"], {"principal": _VALID_PRINCIPAL})
            )
            assert r2.status_code == 409
            body = json.loads(r2.body)
            assert body["code"] == "conflict"
    finally:
        _teardown_org(ids)


@pg_available
@pytest.mark.anyio
async def test_revoked_then_regranted():
    """Revoke then POST same principal → 201 new row (AC3 RGPD trace preserved)."""
    from core.admin_api import (
        _grant_dataset_access,
        _list_dataset_access_grants,
        _revoke_dataset_access,
    )

    suffix = uuid.uuid4().hex[:8]
    ids = _setup_org(suffix)
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            r1 = await _grant_dataset_access(
                _post(ids["org_id"], {"principal": _VALID_PRINCIPAL})
            )
            assert r1.status_code == 201
            grant_id = json.loads(r1.body)["id"]

            rv = await _revoke_dataset_access(_delete(ids["org_id"], grant_id))
            assert rv.status_code == 200

            # Re-grant: must succeed (new row allowed after revoke).
            r2 = await _grant_dataset_access(
                _post(ids["org_id"], {"principal": _VALID_PRINCIPAL})
            )
            assert r2.status_code == 201
            new_grant_id = json.loads(r2.body)["id"]
            assert new_grant_id != grant_id  # new row, not resurrection

            # Only the new grant is active.
            gl = json.loads(
                (await _list_dataset_access_grants(_get(ids["org_id"]))).body
            )["grants"]
            assert len(gl) == 1
            assert gl[0]["id"] == new_grant_id
    finally:
        _teardown_org(ids)


@pg_available
@pytest.mark.anyio
async def test_revoke_sets_revoked_at():
    """DELETE sets revoked_at IS NOT NULL (row kept for RGPD, AC5)."""
    from core.admin_api import _grant_dataset_access, _revoke_dataset_access
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    ids = _setup_org(suffix)
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            r1 = await _grant_dataset_access(
                _post(ids["org_id"], {"principal": _VALID_PRINCIPAL})
            )
            assert r1.status_code == 201
            grant_id = json.loads(r1.body)["id"]

            rv = await _revoke_dataset_access(_delete(ids["org_id"], grant_id))
            assert rv.status_code == 200

        # Verify row still exists with revoked_at set.
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT revoked_at FROM app.dataset_access_grants WHERE id = %s",
                    (grant_id,),
                )
                row = cur.fetchone()
        assert row is not None, "Row must be kept (RGPD soft-delete)"
        assert row[0] is not None, "revoked_at must be set after revoke"
    finally:
        _teardown_org(ids)


@pg_available
@pytest.mark.anyio
async def test_revoke_already_revoked_404():
    """Second DELETE on a revoked grant → 404 (AC5)."""
    from core.admin_api import _grant_dataset_access, _revoke_dataset_access

    suffix = uuid.uuid4().hex[:8]
    ids = _setup_org(suffix)
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            r1 = await _grant_dataset_access(
                _post(ids["org_id"], {"principal": _VALID_PRINCIPAL})
            )
            assert r1.status_code == 201
            grant_id = json.loads(r1.body)["id"]

            rv1 = await _revoke_dataset_access(_delete(ids["org_id"], grant_id))
            assert rv1.status_code == 200
            rv2 = await _revoke_dataset_access(_delete(ids["org_id"], grant_id))
            assert rv2.status_code == 404
            body = json.loads(rv2.body)
            assert body["code"] == "not_found"
    finally:
        _teardown_org(ids)


@pg_available
@pytest.mark.anyio
async def test_non_admin_grant_403():
    """A non-owner/admin identity → 403 forbidden (AC2 gate)."""
    from core.admin_api import _grant_dataset_access
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    ids = _setup_org(suffix)
    # Enrol the org so it's no longer default-open.
    member_id = f"mem_{suffix}"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.org_members "
                "(id, org_id, identity, role, status, invited_by, invited_at, joined_at) "
                "VALUES (%s, %s, %s, 'owner', 'active', 'system', NOW(), NOW())",
                (member_id, ids["org_id"], "owner@example.com"),
            )
        conn.commit()
    try:
        # Non-owner identity attempting a grant.
        with patch(_AUTH[0], return_value=(True, "viewer@example.com")):
            resp = await _grant_dataset_access(
                _post(ids["org_id"], {"principal": _VALID_PRINCIPAL})
            )
        assert resp.status_code == 403
        body = json.loads(resp.body)
        assert body["code"] == "forbidden"
    finally:
        # Cleanup member first (FK), then org.
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM app.org_members WHERE id = %s", (member_id,))
            conn.commit()
        _teardown_org(ids)
