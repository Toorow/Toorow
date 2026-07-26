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
        {"type": "http", "method": "POST", "path": "/api/entry/scope", "headers": headers or []},
        receive,
    )


def test_hosted_entry_api_uses_canonical_person_and_returns_first_project(monkeypatch):
    from core import admin_api, db, entry_confirmations, hosted_entry_scope
    from core.api_auth import ResolvedPrincipal

    principal = ResolvedPrincipal(
        person_id="person_entry",
        issuer="issuer",
        subject="subject",
        verified_email="owner@example.com",
        display_name="Owner",
    )
    monkeypatch.setenv("TOOROW_DEPLOYMENT_MODE", "hosted")
    monkeypatch.setenv("TOOROW_CANONICAL_IDENTITY_ENABLED", "1")
    monkeypatch.setattr(
        admin_api, "_check_canonical_principal", AsyncMock(return_value=(True, principal))
    )
    conn = MagicMock()
    confirmation = entry_confirmations.ConsumedEntryConfirmation(
        confirmation_id="econf_1",
        command_type=entry_confirmations.HOSTED_ENTRY_COMMAND,
        actor_person_id="person_entry",
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

    def create(connection, **kwargs):
        captured["connection"] = connection
        captured.update(kwargs)
        return hosted_entry_scope.HostedEntryScope(
            consumption_id="entryscope_1",
            invitation_id="invite_1",
            person_id="person_entry",
            org_id="org_1",
            project_id="proj_1",
            journey_id="setup_entry",
            operation_id="op_1",
            audit_event_id="audit_1",
            outbox_event_id="outbox_1",
            next_url="/p/proj_1/overview/getting-started",
            replayed=False,
        )

    monkeypatch.setattr(hosted_entry_scope, "create_hosted_entry_scope", create)
    response = asyncio.run(
        admin_api._create_hosted_entry_scope(
            _request(
                {
                    "organization_name": "Acme",
                    "organization_slug": "acme",
                    "project_name": "First project",
                    "project_slug": "first-project",
                },
                headers=[
                    (b"idempotency-key", b"entry-1"),
                    (b"x-confirmation-id", b"econf_1"),
                    (b"x-confirmation-secret", b"ecfs_server_secret"),
                ],
            )
        )
    )

    payload = json.loads(response.body)
    assert response.status_code == 201
    assert captured["connection"] is conn
    assert captured["person_id"] == "person_entry"
    assert captured["confirmation"] is confirmation
    bind_confirmation.assert_called_once()
    assert payload["org_id"] == "org_1"
    assert payload["project_id"] == "proj_1"
    assert payload["journey_id"] == "setup_entry"
    assert payload["next_url"] == "/p/proj_1/overview/getting-started"
    assert response.headers["cache-control"].startswith("no-store")


def test_entry_scope_is_hidden_in_self_hosted_mode(monkeypatch):
    from core import admin_api

    auth = AsyncMock()
    monkeypatch.setenv("TOOROW_DEPLOYMENT_MODE", "self_hosted")
    monkeypatch.setattr(admin_api, "_check_canonical_principal", auth)

    response = asyncio.run(admin_api._create_hosted_entry_scope(_request({})))

    assert response.status_code == 404
    auth.assert_not_awaited()


def test_legacy_org_creation_cannot_bypass_authenticated_entry_or_claim(monkeypatch):
    from core import admin_api

    monkeypatch.setattr(admin_api, "_check_auth", AsyncMock(return_value=(True, "person_1")))
    request = _request({"name": "Bypass"})

    monkeypatch.setenv("TOOROW_DEPLOYMENT_MODE", "hosted")
    monkeypatch.setenv("TOOROW_CANONICAL_IDENTITY_ENABLED", "1")
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    hosted = asyncio.run(admin_api._create_org(request))
    assert hosted.status_code == 409
    assert json.loads(hosted.body)["code"] == "entry_scope_required"

    monkeypatch.setenv("TOOROW_DEPLOYMENT_MODE", "self_hosted")
    self_hosted = asyncio.run(admin_api._create_org(_request({"name": "Bypass"})))
    assert self_hosted.status_code == 404


def test_hosted_entry_route_is_registered():
    from core.admin_api import router

    paths = {route.path for route in router.routes if "POST" in route.methods}
    assert "/api/entry/scope" in paths
    assert "/api/entry/scope/confirmation" in paths


def test_hosted_entry_scope_requires_canonical_identity_activation(monkeypatch):
    from core import admin_api

    monkeypatch.setenv("TOOROW_DEPLOYMENT_MODE", "hosted")
    monkeypatch.delenv("TOOROW_CANONICAL_IDENTITY_ENABLED", raising=False)
    auth = AsyncMock()
    monkeypatch.setattr(admin_api, "_check_canonical_principal", auth)

    response = asyncio.run(admin_api._create_hosted_entry_scope(_request({})))

    assert response.status_code == 503
    assert json.loads(response.body)["code"] == "identity_activation_required"
    auth.assert_not_awaited()


def test_hosted_entry_confirmation_is_server_issued_for_exact_payload(monkeypatch):
    from core import admin_api, db, entry_confirmations
    from core.api_auth import ResolvedPrincipal

    principal = ResolvedPrincipal(
        person_id="person_entry",
        issuer="issuer",
        subject="subject",
        verified_email="owner@example.com",
        display_name="Owner",
    )
    monkeypatch.setenv("TOOROW_DEPLOYMENT_MODE", "hosted")
    monkeypatch.setenv("TOOROW_CANONICAL_IDENTITY_ENABLED", "1")
    monkeypatch.setattr(
        admin_api, "_check_canonical_principal", AsyncMock(return_value=(True, principal))
    )

    @contextmanager
    def get_connection():
        yield MagicMock()

    monkeypatch.setattr(db, "get_connection", get_connection)
    issue = MagicMock(
        return_value=entry_confirmations.IssuedEntryConfirmation(
            confirmation_id="econf_issued",
            confirmation_secret="ecfs_server_secret",
            command_type=entry_confirmations.HOSTED_ENTRY_COMMAND,
            payload_hash="a" * 64,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
        )
    )
    monkeypatch.setattr(entry_confirmations, "issue_entry_confirmation", issue)
    body = {
        "organization_name": "Acme",
        "organization_slug": "acme",
        "project_name": "First project",
        "project_slug": "first-project",
        "currency": "EUR",
        "timezone": "Europe/Paris",
    }

    response = asyncio.run(
        admin_api._issue_hosted_entry_confirmation(
            _request(body, headers=[(b"idempotency-key", b"entry-1")])
        )
    )

    assert response.status_code == 201
    payload = json.loads(response.body)
    assert payload["confirmation_id"] == "econf_issued"
    assert payload["confirmation_secret"] == "ecfs_server_secret"
    assert response.headers["cache-control"].startswith("no-store")
    assert issue.call_args.kwargs["actor_person_id"] == "person_entry"
    assert issue.call_args.kwargs["request_payload"] == body
