"""Story 36.4 atomic matching-identity invitation acceptance tests."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.requests import Request


def _request(path: str, body: dict, *, headers=(), cookies=None) -> Request:
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {
            "type": "http.request",
            "body": json.dumps(body).encode(),
            "more_body": False,
        }

    raw_headers = list(headers)
    if cookies:
        raw_headers.append(
            (b"cookie", "; ".join(f"{key}={value}" for key, value in cookies.items()).encode())
        )
    return Request(
        {"type": "http", "method": "POST", "path": path, "headers": raw_headers},
        receive,
    )


def _conn(*rows):
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.side_effect = list(rows)
    cur.rowcount = 1
    conn.cursor.return_value = cur
    return conn, cur


def test_exchange_requires_exact_identity_and_persists_only_hashes(monkeypatch):
    from core import invitations

    monkeypatch.setenv("TOOROW_INVITATION_PEPPER", "p" * 32)
    identity_hash = invitations.prepare_identity_binding("user@example.com").identity_hash
    bearer = "b" * 48
    bearer_hash = invitations._bearer_hash(bearer)
    conn, cur = _conn(
        (
            "invite-1",
            identity_hash,
            "delivered",
            datetime.now(timezone.utc) + timedelta(hours=1),
            None,
            bearer_hash,
            None,
            "policy-v1",
        )
    )

    result = invitations.exchange_invitation(
        conn, bearer=bearer, verified_identity="User@example.com"
    )

    assert result.session_value
    assert result.session_value not in repr(cur.execute.call_args_list)
    assert bearer not in repr(cur.execute.call_args_list)
    sql = " ".join(call.args[0] for call in cur.execute.call_args_list)
    assert "FOR UPDATE" in sql
    assert "bearer_consumed_at" in sql
    assert "INSERT INTO app.invitation_exchange_sessions" in sql
    conn.commit.assert_not_called()


def test_exchange_identity_mismatch_is_nondisclosing_and_writes_nothing(monkeypatch):
    from core import invitations

    monkeypatch.setenv("TOOROW_INVITATION_PEPPER", "p" * 32)
    conn, cur = _conn(
        (
            "invite-1",
            invitations.prepare_identity_binding("other@example.com").identity_hash,
            "pending",
            datetime.now(timezone.utc) + timedelta(hours=1),
            None,
            invitations._bearer_hash("b" * 48),
            None,
            "policy-v1",
        )
    )

    with pytest.raises(invitations.InvitationExchangeError, match="unavailable"):
        invitations.exchange_invitation(conn, bearer="b" * 48, verified_identity="user@example.com")
    assert len(cur.execute.call_args_list) == 1


def test_acceptance_materializes_exact_role_and_grants_atomically(monkeypatch):
    from core import invitations, operations, setup_responsibilities

    monkeypatch.setenv("TOOROW_INVITATION_PEPPER", "p" * 32)
    monkeypatch.setattr(
        setup_responsibilities,
        "bootstrap_journey_from_acceptance",
        lambda *_a, **_k: "setup-1",
    )
    identity_hash = invitations.prepare_identity_binding("user@example.com").identity_hash
    conn, cur = _conn(
        (
            "ixs-1",
            "invite-1",
            identity_hash,
            datetime.now(timezone.utc) + timedelta(minutes=5),
            None,
            None,
            "org-1",
            "member",
            [
                {"scope_type": "project", "scope_id": "proj-1", "capability": "view"},
                {"scope_type": "flux", "scope_id": "flux-1", "capability": "edit"},
            ],
            "pending",
            datetime.now(timezone.utc) + timedelta(hours=1),
            None,
            "policy-v1",
        ),
        (1,),
        (1,),
        (0,),
        None,
    )
    captured = {}

    def execute(operation_conn, spec, *, mutation):
        captured["spec"] = spec
        changed = mutation(operation_conn, "op-accept")
        captured["changed"] = changed
        return operations.OperationResult(
            "op-accept", "succeeded", changed.result, "audit-1", "outbox-1", False
        )

    monkeypatch.setattr(invitations, "execute_operation", execute)
    result = invitations.accept_invitation(
        conn,
        session_value="s" * 48,
        verified_identity="user@example.com",
        confirmed=True,
        idempotency_key="accept-1",
        host_context={"host": "rest"},
        trace_id=None,
    )

    assert result.role == "member"
    assert result.explicit_none is False
    assert result.next_url == "/onboarding/responsibilities"
    sql = " ".join(call.args[0] for call in cur.execute.call_args_list)
    assert "INSERT INTO app.org_members" in sql
    assert sql.count("INSERT INTO app.resource_grants") == 2
    assert "SET state = 'accepted'" in sql
    assert "INSERT INTO app.operation_outbox" not in sql
    assert captured["changed"].outbox_payload == {
        "invitation_id": "invite-1",
        "transition": "accepted",
    }
    conn.commit.assert_not_called()


def test_exchange_api_sets_only_strict_http_only_cookie(monkeypatch):
    from core import admin_api, db, invitations, project_access

    conn, _ = _conn()

    @contextmanager
    def get_connection():
        yield conn

    monkeypatch.setenv("TOOROW_AUTH_MODE", "static")
    monkeypatch.setattr(db, "get_connection", get_connection)
    monkeypatch.setattr(
        admin_api, "_check_invitation_identity", AsyncMock(return_value=(True, "user@example.com"))
    )
    monkeypatch.setattr(project_access, "epic36_production_access_enabled", lambda: True)
    monkeypatch.setattr(
        invitations,
        "exchange_invitation",
        lambda *_a, **_k: invitations.InvitationExchangeResult("session-secret", 600),
    )

    response = asyncio.run(
        admin_api._exchange_invitation(
            _request("/api/invitations/exchange", {"bearer": "raw-secret"})
        )
    )

    assert response.status_code == 200
    assert json.loads(response.body) == {"ready_to_accept": True}
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=strict" in cookie
    assert "raw-secret" not in repr(response.headers)
    assert response.headers["cache-control"].startswith("no-store")
    conn.commit.assert_called_once()


def test_accept_api_returns_tokenless_scope_context_and_clears_cookie(monkeypatch):
    from core import admin_api, db, invitations, project_access

    conn, _ = _conn()

    @contextmanager
    def get_connection():
        yield conn

    monkeypatch.setenv("TOOROW_AUTH_MODE", "static")
    monkeypatch.setattr(db, "get_connection", get_connection)
    monkeypatch.setattr(
        admin_api, "_check_invitation_identity", AsyncMock(return_value=(True, "user@example.com"))
    )
    monkeypatch.setattr(project_access, "epic36_production_access_enabled", lambda: True)
    monkeypatch.setattr(
        invitations,
        "accept_invitation",
        lambda *_a, **_k: invitations.InvitationAcceptanceResult(
            invitation_id="invite-1",
            org_id="org-1",
            role="viewer",
            explicit_grants=(),
            explicit_none=True,
            operation_id="op-1",
            audit_event_id="audit-1",
            outbox_event_id="outbox-1",
            next_url="/onboarding/responsibilities",
            replayed=False,
        ),
    )

    response = asyncio.run(
        admin_api._accept_invitation(
            _request(
                "/api/invitations/accept",
                {"confirmed": True},
                headers=[(b"idempotency-key", b"accept-1")],
                cookies={"toorow_invitation_exchange": "session-secret"},
            )
        )
    )

    body = json.loads(response.body)
    assert body["authority"]["role_derived"] == "viewer"
    assert body["authority"]["explicit_none"] is True
    assert body["next_url"] == "/onboarding/responsibilities"
    assert "session-secret" not in response.body.decode()
    assert "max-age=0" in response.headers["set-cookie"].lower()
    conn.commit.assert_called_once()


def test_migration_063_contains_one_time_exchange_contract():
    from pathlib import Path

    sql = Path("infra/nango/migrations/063_invitation_acceptance.sql").read_text()
    assert "invitation_exchange_sessions" in sql
    assert "session_hash" in sql
    assert "bearer_consumed_at" in sql
    assert "UNIQUE (invitation_id)" in sql
    assert "raw_bearer" not in sql


def test_invitation_exchange_and_accept_routes_are_registered():
    from core.admin_api import router

    paths = {route.path for route in router.routes}
    assert "/api/invitations/exchange" in paths
    assert "/api/invitations/accept" in paths


def test_invitation_auth_uses_only_verified_email_claim(monkeypatch):
    from core import api_auth

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/invitations/exchange",
            "headers": [(b"authorization", b"Bearer valid-token")],
        }
    )
    access = MagicMock()
    access.claims = {
        "email": "Verified.User@example.com",
        "email_verified": True,
    }
    access.subject = "opaque-provider-subject"
    access.client_id = "client-1"
    verifier = MagicMock()
    verifier.verify_token = AsyncMock(return_value=access)
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setattr(api_auth, "_verifier", lambda: verifier)

    assert asyncio.run(api_auth.authenticate_invitation_identity(request)) == (
        True,
        "Verified.User@example.com",
    )

    access.claims["email_verified"] = False
    assert asyncio.run(api_auth.authenticate_invitation_identity(request)) == (False, "")


def test_consumed_acceptance_returns_resolved_state_without_replaying_effects(monkeypatch):
    from core import invitations

    monkeypatch.setenv("TOOROW_INVITATION_PEPPER", "p" * 32)
    identity_hash = invitations.prepare_identity_binding("user@example.com").identity_hash
    resolution = {
        "invitation_id": "invite-1",
        "org_id": "org-1",
        "role": "viewer",
        "explicit_grants": [],
        "next_url": "/onboarding/responsibilities",
    }
    conn, cur = _conn(
        (
            "ixs-1",
            "invite-1",
            identity_hash,
            datetime.now(timezone.utc) + timedelta(minutes=5),
            datetime.now(timezone.utc),
            "op-accept",
            "org-1",
            "viewer",
            [],
            "accepted",
            datetime.now(timezone.utc) + timedelta(hours=1),
            None,
            "policy-v1",
            resolution,
        ),
        ("succeeded", resolution, "audit-1", "outbox-1"),
    )

    result = invitations.accept_invitation(
        conn,
        session_value="s" * 48,
        verified_identity="user@example.com",
        confirmed=True,
        idempotency_key="different-retry-key",
        host_context={"host": "rest"},
        trace_id=None,
    )

    assert result.replayed is True
    assert result.explicit_none is True
    sql = " ".join(call.args[0] for call in cur.execute.call_args_list)
    assert "INSERT INTO app.org_members" not in sql
    assert "INSERT INTO app.resource_grants" not in sql
