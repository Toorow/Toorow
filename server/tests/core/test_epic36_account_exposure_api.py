"""Story 36.2 REST adoption of the account-exposure operation seam."""

import asyncio
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock

from starlette.requests import Request


def _request(headers: list[tuple[bytes, bytes]]) -> Request:
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {
            "type": "http.request",
            "body": b'{"grantee_org_id":"org-beneficiary"}',
            "more_body": False,
        }

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": headers,
            "path_params": {
                "credential_id": "conn-1",
                "external_account_id": "acct-1",
            },
        },
        receive,
    )


def test_rest_exposure_uses_atomic_operation_and_skips_best_effort_audit(monkeypatch):
    from core import account_exposure, admin_api, db, project_access
    from core.operations import OperationResult

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.side_effect = [(1,), ("org-owner",), (1,)]
    conn.cursor.return_value = cur

    @contextmanager
    def get_connection():
        yield conn

    monkeypatch.setattr(db, "get_connection", get_connection)
    monkeypatch.setattr(
        admin_api, "_check_auth", AsyncMock(return_value=(True, "owner-1"))
    )
    monkeypatch.setattr(admin_api, "_enforce_org_manage", lambda *_a: None)
    monkeypatch.setattr(project_access, "epic36_production_access_enabled", lambda: True)
    best_effort = MagicMock()
    monkeypatch.setattr(admin_api, "write_audit_row", best_effort)
    operation = MagicMock(
        return_value=OperationResult(
            operation_id="op-1",
            outcome="succeeded",
            result={
                "id": "grant-1",
                "credential_id": "conn-1",
                "external_account_id": "acct-1",
                "grantee_org_id": "org-beneficiary",
                "granted_by": "owner-1",
                "created_at": None,
            },
            audit_event_id="audit-1",
            outbox_event_id="opout-1",
            replayed=False,
        )
    )
    monkeypatch.setattr(account_exposure, "expose_account", operation)

    response = asyncio.run(
        admin_api._create_account_grant(
            _request(
                [
                    (b"idempotency-key", b"request-1"),
                    (b"x-confirmation-reference", b"confirm-1"),
                ]
            )
        )
    )

    assert response.status_code == 201
    assert b'"operation_id":"op-1"' in response.body
    assert b'"audit_event_id":"audit-1"' in response.body
    best_effort.assert_not_called()
    conn.commit.assert_called_once()
    assert operation.call_args.kwargs["idempotency_key"] == "request-1"


def test_rest_exposure_requires_idempotency_key_when_gate_is_enabled(monkeypatch):
    from core import admin_api, db, project_access

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.side_effect = [(1,), ("org-owner",), (1,)]
    conn.cursor.return_value = cur

    @contextmanager
    def get_connection():
        yield conn

    monkeypatch.setattr(db, "get_connection", get_connection)
    monkeypatch.setattr(
        admin_api, "_check_auth", AsyncMock(return_value=(True, "owner-1"))
    )
    monkeypatch.setattr(admin_api, "_enforce_org_manage", lambda *_a: None)
    monkeypatch.setattr(project_access, "epic36_production_access_enabled", lambda: True)
    response = asyncio.run(admin_api._create_account_grant(_request([])))
    assert response.status_code == 422
    assert b"missing_idempotency_key" in response.body
    conn.commit.assert_not_called()
