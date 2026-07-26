from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from starlette.requests import Request


def _request(body: dict, *, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
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

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/instance/claim",
            "headers": headers or [],
        },
        receive,
    )


def test_claim_api_uses_canonical_person_and_returns_tokenless_scope(monkeypatch):
    from core import admin_api, db, entry_confirmations, self_hosted_instance_claim
    from core.api_auth import ResolvedPrincipal

    principal = ResolvedPrincipal(
        person_id="person_1",
        issuer="issuer-1",
        subject="subject-1",
        verified_email="owner@example.com",
        display_name="Owner",
    )
    monkeypatch.setenv("TOOROW_DEPLOYMENT_MODE", "self_hosted")
    monkeypatch.setenv("TOOROW_CANONICAL_IDENTITY_ENABLED", "1")
    monkeypatch.setattr(
        admin_api,
        "_check_canonical_principal",
        AsyncMock(return_value=(True, principal)),
    )
    conn = MagicMock()
    confirmation = entry_confirmations.ConsumedEntryConfirmation(
        confirmation_id="econf_claim",
        command_type=entry_confirmations.INSTANCE_CLAIM_COMMAND,
        actor_person_id="person_1",
        payload_hash="a" * 64,
        idempotency_key_hash="b" * 64,
        operation_id=None,
        replayed=False,
    )
    monkeypatch.setattr(
        entry_confirmations, "consume_entry_confirmation", MagicMock(return_value=confirmation)
    )
    bind_confirmation = MagicMock()
    monkeypatch.setattr(
        entry_confirmations, "bind_entry_confirmation_operation", bind_confirmation
    )

    @contextmanager
    def get_connection():
        yield conn

    monkeypatch.setattr(db, "get_connection", get_connection)
    captured = {}

    def claim(connection, **kwargs):
        captured["connection"] = connection
        captured.update(kwargs)
        return self_hosted_instance_claim.SelfHostedClaim(
            claim_id="claim-1",
            person_id="person_1",
            org_id="org-1",
            project_id="project-1",
            journey_id="setup_claim",
            operation_id="op-1",
            audit_event_id="audit-1",
            outbox_event_id="outbox-1",
            replayed=False,
        )

    monkeypatch.setattr(self_hosted_instance_claim, "claim_self_hosted_instance", claim)
    response = asyncio.run(
        admin_api._claim_self_hosted_instance(
            _request(
                {
                    "organization_name": "Acme",
                    "organization_slug": "acme",
                    "project_name": "First project",
                    "project_slug": "first-project",
                },
                headers=[
                    (b"idempotency-key", b"claim-1"),
                    (b"x-confirmation-id", b"econf_claim"),
                    (b"x-confirmation-secret", b"ecfs_server_secret"),
                    (b"cookie", b"toorow_instance_bootstrap_exchange=" + b"s" * 48),
                ],
            )
        )
    )

    payload = json.loads(response.body)
    assert response.status_code == 201
    assert captured["connection"] is conn
    assert captured["claimant_person_id"] == "person_1"
    assert captured["confirmation"] is confirmation
    bind_confirmation.assert_called_once()
    assert captured["bootstrap_exchange_bearer"] == "s" * 48
    assert payload["next_url"] == "/p/project-1/overview/getting-started"
    assert "bootstrap" not in payload
    assert response.headers["cache-control"].startswith("no-store")


def test_bootstrap_exchange_sets_only_a_short_strict_cookie(monkeypatch):
    from core import admin_api, db, self_hosted_instance_claim

    monkeypatch.setenv("TOOROW_DEPLOYMENT_MODE", "self_hosted")
    conn = MagicMock()

    @contextmanager
    def get_connection():
        yield conn

    monkeypatch.setattr(db, "get_connection", get_connection)
    captured = {}

    def exchange(connection, **kwargs):
        captured["connection"] = connection
        captured.update(kwargs)
        return self_hosted_instance_claim.BootstrapExchange(
            exchange_id="exchange-1",
            session_bearer="s" * 48,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )

    monkeypatch.setattr(
        self_hosted_instance_claim,
        "exchange_bootstrap_capability",
        exchange,
    )
    response = asyncio.run(
        admin_api._exchange_instance_bootstrap(
            _request({"bootstrap_bearer": "b" * 48})
        )
    )

    assert response.status_code == 200
    assert json.loads(response.body) == {"ready_to_claim": True}
    assert captured["bootstrap_bearer"] == "b" * 48
    assert "b" * 48 not in response.body.decode()
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=strict" in cookie
    assert "path=/api/instance/claim" in cookie

def test_claim_api_is_hidden_outside_self_hosted_mode(monkeypatch):
    from core import admin_api

    auth = AsyncMock()
    monkeypatch.setenv("TOOROW_DEPLOYMENT_MODE", "hosted")
    monkeypatch.setattr(admin_api, "_check_canonical_principal", auth)

    response = asyncio.run(admin_api._claim_self_hosted_instance(_request({})))

    assert response.status_code == 404
    auth.assert_not_awaited()


def test_claim_session_resumes_from_http_only_cookie(monkeypatch):
    from core import admin_api, db, self_hosted_instance_claim

    monkeypatch.setenv("TOOROW_DEPLOYMENT_MODE", "self_hosted")
    conn = MagicMock()

    @contextmanager
    def get_connection():
        yield conn

    monkeypatch.setattr(db, "get_connection", get_connection)
    check = MagicMock(return_value=True)
    monkeypatch.setattr(
        self_hosted_instance_claim,
        "bootstrap_exchange_session_is_ready",
        check,
    )
    request = _request(
        {},
        headers=[(b"cookie", b"toorow_instance_bootstrap_exchange=" + b"s" * 48)],
    )

    response = asyncio.run(admin_api._get_self_hosted_claim_session(request))

    assert response.status_code == 200
    assert json.loads(response.body) == {"ready_to_claim": True}
    assert response.headers["cache-control"].startswith("no-store")
    assert check.call_args.kwargs["bootstrap_exchange_bearer"] == "s" * 48


def test_claim_session_is_nondisclosing_when_cookie_is_not_ready(monkeypatch):
    from core import admin_api, db, self_hosted_instance_claim

    monkeypatch.setenv("TOOROW_DEPLOYMENT_MODE", "self_hosted")

    @contextmanager
    def get_connection():
        yield MagicMock()

    monkeypatch.setattr(db, "get_connection", get_connection)
    monkeypatch.setattr(
        self_hosted_instance_claim,
        "bootstrap_exchange_session_is_ready",
        MagicMock(return_value=False),
    )
    request = _request(
        {},
        headers=[(b"cookie", b"toorow_instance_bootstrap_exchange=" + b"s" * 48)],
    )

    response = asyncio.run(admin_api._get_self_hosted_claim_session(request))

    assert response.status_code == 404
    assert response.headers["cache-control"].startswith("no-store")


def test_claim_route_is_registered():
    from core.admin_api import router

    post_paths = {route.path for route in router.routes if "POST" in route.methods}
    get_paths = {route.path for route in router.routes if "GET" in route.methods}
    assert "/api/instance/bootstrap/exchange" in post_paths
    assert "/api/instance/claim" in post_paths
    assert "/api/instance/claim/confirmation" in post_paths
    assert "/api/instance/claim/session" in get_paths


def test_instance_claim_requires_canonical_identity_activation(monkeypatch):
    from core import admin_api

    monkeypatch.setenv("TOOROW_DEPLOYMENT_MODE", "self_hosted")
    monkeypatch.delenv("TOOROW_CANONICAL_IDENTITY_ENABLED", raising=False)
    auth = AsyncMock()
    monkeypatch.setattr(admin_api, "_check_canonical_principal", auth)

    response = asyncio.run(admin_api._claim_self_hosted_instance(_request({})))

    assert response.status_code == 503
    assert json.loads(response.body)["code"] == "identity_activation_required"
    auth.assert_not_awaited()
