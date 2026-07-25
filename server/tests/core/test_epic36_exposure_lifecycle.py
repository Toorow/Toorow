"""Story 36.1 durable exposure re-approval tests."""

import asyncio
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock

from starlette.requests import Request


def _request(path_params: dict[str, str], body: bytes) -> Request:
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "path_params": path_params,
        },
        receive,
    )


def test_explicit_reapproval_reactivates_and_versions_invalidated_grant(monkeypatch):
    from core import admin_api, db

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.side_effect = [
        (1,),
        ("org-owner",),
        (1,),
        ("cgrant-1", "invalidated"),
        ("cgrant-1", "conn-1", "acct-1", "org-beneficiary", "owner-1", None),
    ]
    conn.cursor.return_value = cur

    @contextmanager
    def get_connection():
        yield conn

    monkeypatch.setattr(db, "get_connection", get_connection)
    monkeypatch.setattr(
        admin_api, "_check_auth", AsyncMock(return_value=(True, "owner-1"))
    )
    monkeypatch.setattr(admin_api, "_enforce_org_manage", lambda *_a: None)
    monkeypatch.setattr(admin_api, "write_audit_row", MagicMock())

    response = asyncio.run(
        admin_api._create_account_grant(
            _request(
                {"credential_id": "conn-1", "external_account_id": "acct-1"},
                b'{"grantee_org_id":"org-beneficiary"}',
            )
        )
    )

    assert response.status_code == 201
    sql = " ".join(call.args[0] for call in cur.execute.call_args_list)
    assert "SET status = 'active'" in sql
    assert "exposure_version = exposure_version + 1" in sql
    conn.commit.assert_called_once()
