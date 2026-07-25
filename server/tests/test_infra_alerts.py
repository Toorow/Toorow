"""Tests for AI-41: nango_revoke_failed infra alert during project archival.

Also covers write_infra_firing helper in infra_alerts.py (used by both AI-32 and AI-41).

Covers:
  * on Nango revocation failure during archival, write_infra_firing is called
    with alert_type='nango_revoke_failed' and project_id in metadata (AI-41)
  * archival still completes (returns 200) even when revocation raises (best-effort)
  * write_infra_firing does NOT emit nango_revoke_failed when revocation succeeds
  * write_infra_firing in infra_alerts.py writes the correct type to alert_firings
  * write_infra_firing never raises (best-effort)

All tests are mock-based (no live DB, no network calls).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run_async(coro):
    """Run an async coroutine synchronously.

    asyncio.run creates a fresh event loop per call: get_event_loop() breaks
    when an earlier async test in the full suite has closed the current loop.
    """
    return asyncio.run(coro)


def _make_delete_project_request(project_id: str = "proj_test"):
    """Build a minimal mock Starlette Request for DELETE /api/projects/{id}."""
    req = MagicMock()
    req.path_params = {"project_id": project_id}
    req.body = AsyncMock(return_value=b"")
    return req


def _run_delete_project(
    project_id: str = "proj_test",
    revoked_rows: list | None = None,
    revoke_side_effect=None,
    mock_write_firing: MagicMock | None = None,
):
    """Drive _delete_project with controlled mocks; return the HTTP response."""
    from core.admin_api import _delete_project

    if revoked_rows is None:
        revoked_rows = [("conn_ABC", "meta-ads", "nango_conn_1")]

    # Psycopg mock: cursor returns active project status + revoked rows.
    mock_cur = MagicMock()
    mock_cur.fetchone.return_value = ("active",)
    mock_cur.fetchall.return_value = revoked_rows
    mock_cur.__enter__ = lambda s: s
    mock_cur.__exit__ = MagicMock(return_value=False)

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__ = lambda s: s
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.commit = MagicMock()

    # nango_client mock.
    mock_nango = MagicMock()
    if revoke_side_effect is not None:
        mock_nango.revoke_connection.side_effect = revoke_side_effect
    else:
        mock_nango.revoke_connection.return_value = None

    # Tenant key mock (avoids import errors inside the handler).
    mock_tenant_mod = MagicMock()
    mock_tenant_mod.get_tenant_key_backend.return_value = MagicMock()
    mock_tenant_mod.write_key_audit_row = MagicMock()

    firing_mock = mock_write_firing if mock_write_firing is not None else MagicMock()

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "test-user"))),
        patch("core.db.get_connection", MagicMock(return_value=mock_conn)),
        patch("core.admin_api.nango_client", mock_nango),
        patch("core.admin_api.write_audit_row", MagicMock()),
        patch("core.infra_alerts.write_infra_firing", firing_mock),
        patch.dict("sys.modules", {"core.tenant_keys": mock_tenant_mod}),
    ):
        req = _make_delete_project_request(project_id)
        return _run_async(_delete_project(req)), firing_mock


# ---------------------------------------------------------------------------
# AI-41 -- nango_revoke_failed firing
# ---------------------------------------------------------------------------


def test_nango_revoke_failed_firing_emitted_on_revocation_error():
    """write_infra_firing called with alert_type='nango_revoke_failed' when revoke raises."""
    mock_write_firing = MagicMock()
    response, mock_write_firing = _run_delete_project(
        revoke_side_effect=RuntimeError("Nango 503"),
        mock_write_firing=mock_write_firing,
    )

    # The firing must have been emitted.
    assert mock_write_firing.called, "Expected write_infra_firing to be called"
    found = any(
        (
            call.kwargs.get("alert_type") == "nango_revoke_failed"
            or (call.args and call.args[0] == "nango_revoke_failed")
        )
        for call in mock_write_firing.call_args_list
    )
    assert found, (
        f"Expected write_infra_firing called with alert_type='nango_revoke_failed'. "
        f"Actual calls: {mock_write_firing.call_args_list}"
    )


def test_nango_revoke_failed_firing_includes_project_id():
    """nango_revoke_failed firing metadata includes the project_id."""
    mock_write_firing = MagicMock()
    _run_delete_project(
        project_id="proj_xyz",
        revoke_side_effect=RuntimeError("timeout"),
        mock_write_firing=mock_write_firing,
    )

    # Find the nango_revoke_failed call and check project_id in kwargs.
    nango_fail_calls = [
        c for c in mock_write_firing.call_args_list
        if (
            c.kwargs.get("alert_type") == "nango_revoke_failed"
            or (c.args and c.args[0] == "nango_revoke_failed")
        )
    ]
    assert nango_fail_calls, "Expected nango_revoke_failed firing"
    call = nango_fail_calls[0]
    # project_id must appear either as kwarg or in metadata.
    project_id_kwarg = call.kwargs.get("project_id")
    metadata = call.kwargs.get("metadata") or {}
    assert project_id_kwarg == "proj_xyz" or metadata.get("project_id") == "proj_xyz", (
        f"Expected project_id='proj_xyz' in firing call, got kwargs={call.kwargs}"
    )


def test_archival_succeeds_when_revocation_raises():
    """Archival returns 200 even when Nango revocation raises (best-effort)."""
    response, _ = _run_delete_project(
        revoke_side_effect=ConnectionError("timeout"),
    )
    assert response.status_code == 200, (
        f"Expected 200 from archival despite revoke failure, got {response.status_code}"
    )


def test_no_nango_revoke_firing_when_revocation_succeeds():
    """write_infra_firing for nango_revoke_failed NOT called when revocation succeeds."""
    mock_write_firing = MagicMock()
    response, mock_write_firing = _run_delete_project(
        revoke_side_effect=None,  # success
        mock_write_firing=mock_write_firing,
    )

    assert response.status_code == 200
    nango_fail_calls = [
        c for c in mock_write_firing.call_args_list
        if (
            c.kwargs.get("alert_type") == "nango_revoke_failed"
            or (c.args and c.args[0] == "nango_revoke_failed")
        )
    ]
    assert not nango_fail_calls, (
        f"Expected no nango_revoke_failed firing on success, got {nango_fail_calls}"
    )


# ---------------------------------------------------------------------------
# write_infra_firing unit tests (infra_alerts.py)
# ---------------------------------------------------------------------------


def test_write_infra_firing_inserts_correct_type():
    """write_infra_firing writes the given alert_type to app.alert_firings."""
    from core.infra_alerts import write_infra_firing

    mock_cur = MagicMock()
    mock_cur.__enter__ = lambda s: s
    mock_cur.__exit__ = MagicMock(return_value=False)

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__ = lambda s: s
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.commit = MagicMock()

    with (
        patch("core.db.get_connection", MagicMock(return_value=mock_conn)),
        patch.dict("sys.modules", {"ulid": MagicMock(ULID=MagicMock(return_value="FAKEID"))}),
    ):
        write_infra_firing(
            alert_type="nango_revoke_failed",
            project_id="proj_x",
            metric="nango_revoke",
            severity="error",
            message="test revoke failure",
            metadata={"connection_ref_id": "conn_123"},
        )

    assert mock_cur.execute.called, "Expected cursor.execute to be called"
    call_args = mock_cur.execute.call_args
    sql = call_args.args[0] if call_args.args else ""
    params = call_args.args[1] if len(call_args.args) > 1 else ()
    assert "INSERT INTO app.alert_firings" in sql, f"Expected INSERT statement, got: {sql!r}"
    assert "nango_revoke_failed" in params, (
        f"Expected 'nango_revoke_failed' in INSERT params, got {params}"
    )


def test_write_infra_firing_scheduler_step_degraded():
    """write_infra_firing correctly handles scheduler_step_degraded type."""
    from core.infra_alerts import write_infra_firing

    mock_cur = MagicMock()
    mock_cur.__enter__ = lambda s: s
    mock_cur.__exit__ = MagicMock(return_value=False)

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__ = lambda s: s
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.commit = MagicMock()

    with (
        patch("core.db.get_connection", MagicMock(return_value=mock_conn)),
        patch.dict("sys.modules", {"ulid": MagicMock(ULID=MagicMock(return_value="FAKEID2"))}),
    ):
        write_infra_firing(
            alert_type="scheduler_step_degraded",
            project_id="default",
            metric="scheduler_health",
            severity="error",
            message="steps degraded",
            metadata={"degraded_steps": ["dispatch_nightly"]},
        )

    params = mock_cur.execute.call_args.args[1]
    assert "scheduler_step_degraded" in params, (
        f"Expected 'scheduler_step_degraded' in INSERT params, got {params}"
    )


def test_write_infra_firing_never_raises_on_db_error():
    """write_infra_firing swallows DB exceptions (best-effort, never raises)."""
    from core.infra_alerts import write_infra_firing

    with patch("core.db.get_connection", side_effect=Exception("DB down")):
        # Must not raise.
        write_infra_firing(
            alert_type="scheduler_step_degraded",
            message="some step failed",
        )


def test_write_infra_firing_never_raises_on_missing_ulid():
    """write_infra_firing swallows ImportError for ulid (best-effort)."""
    from core.infra_alerts import write_infra_firing

    with patch.dict("sys.modules", {"ulid": None}):
        # Must not raise.
        write_infra_firing(
            alert_type="nango_revoke_failed",
            message="ulid missing",
        )
