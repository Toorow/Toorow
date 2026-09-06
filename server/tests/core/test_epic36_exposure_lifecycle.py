"""Story 36.1 durable exposure re-approval tests."""

import asyncio
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock

from core import credential_accounts_api  # AD-43 : le handler vit chez son sujet
from starlette.requests import Request


def _request(
    path_params: dict[str, str], body: bytes, *, idempotency_key: str = "exposure-test-1"
) -> Request:
    """A consequential command CARRIES ITS IDEMPOTENCY KEY, and this sent none.

    `_create_account_grant` refuses without one -- `missing_idempotency_key`,
    "Idempotency-Key is required" -- so the handler answered 422 before reaching
    anything this test is about, and the refusal named the missing header the
    whole time. The key is a parameter rather than a constant so a test that
    wants to prove the refusal itself can still send none.
    """
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
            "headers": (
                [(b"idempotency-key", idempotency_key.encode())] if idempotency_key else []
            ),
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
    # THE CURSOR ANSWERS BY QUERY, NOT BY POSITION. This was a positional list of
    # five, and the handler gained an idempotency-ledger read in the middle: the
    # fourth answer -- a 2-tuple meant for the grant lookup -- landed on a SELECT
    # of six columns and raised `tuple index out of range`, surfacing as a 500.
    # A list pinned to call order says nothing about the behaviour it defends and
    # goes red the day a read is inserted anywhere before its end.
    def _answer():
        sql = str(cur.execute.call_args_list[-1].args[0])
        if "app.credential_accounts" in sql:
            return (1,)
        if "app.connection_ref" in sql:
            return ("org-owner",)
        if "app.organizations" in sql:
            return (1,)
        if "outbox_event_id" in sql:
            # No prior record under this key: the command runs for the first time.
            return None
        if "SELECT id, status" in sql or "status FROM app.credential_account_grants" in sql:
            return ("cgrant-1", "invalidated")
        return ("cgrant-1", "conn-1", "acct-1", "org-beneficiary", "owner-1", None)

    cur.fetchone.side_effect = _answer
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
        credential_accounts_api._create_account_grant(
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
