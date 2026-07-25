"""Story 36.2 adoption of the shared operation seam by account exposure."""

from datetime import datetime, timezone
from unittest.mock import MagicMock


def test_account_exposure_uses_shared_operation_and_reactivates_explicitly(monkeypatch):
    from core import account_exposure, operations

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.side_effect = [
        ("grant-1", "invalidated"),
        (
            "grant-1",
            "conn-1",
            "acct-1",
            "org-2",
            "owner-1",
            datetime(2026, 7, 22, tzinfo=timezone.utc),
        ),
    ]
    conn.cursor.return_value = cur
    captured = {}

    def execute(operation_conn, spec, *, mutation):
        captured["spec"] = spec
        changed = mutation(operation_conn, "op-1")
        captured["changed"] = changed
        return operations.OperationResult(
            "op-1", changed.outcome, changed.result, "audit-1", "opout-1", False
        )

    monkeypatch.setattr(account_exposure, "execute_operation", execute)
    result = account_exposure.expose_account(
        conn,
        grant_id="grant-new",
        credential_id="conn-1",
        external_account_id="acct-1",
        owner_org_id="org-1",
        grantee_org_id="org-2",
        actor="owner-1",
        idempotency_key="request-1",
        host_context={"host": "console", "workspace_id": "rest"},
        versions={"policy": "v1", "catalog": "v1", "tool": "rest-v1"},
        confirmation_reference="confirm-1",
        trace_id=None,
    )
    assert result.operation_id == "op-1"
    assert captured["spec"].resource_path[-1] == "beneficiary:org-2"
    assert captured["changed"].result["created_at"].endswith("+00:00")
    sql = " ".join(call.args[0] for call in cur.execute.call_args_list)
    assert "exposure_version = exposure_version + 1" in sql
