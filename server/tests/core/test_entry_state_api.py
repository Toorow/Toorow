from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock

from starlette.requests import Request


def _request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/api/entry-state", "headers": []})


def _principal():
    from core.api_auth import ResolvedPrincipal

    return ResolvedPrincipal(
        person_id="person_1",
        issuer="issuer",
        subject="subject",
        verified_email="owner@example.com",
        display_name="Owner",
    )


def _install_db(monkeypatch, row):
    from core import db

    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    cursor.fetchone.return_value = row
    conn = MagicMock()
    conn.cursor.return_value = cursor

    @contextmanager
    def get_connection():
        yield conn

    monkeypatch.setattr(db, "get_connection", get_connection)
    return cursor


def test_hosted_entry_state_distinguishes_entitlement_from_uninvited(monkeypatch):
    from core import admin_api

    monkeypatch.setenv("TOOROW_DEPLOYMENT_MODE", "hosted")
    monkeypatch.setenv("TOOROW_AUTH_MODE", "static")
    monkeypatch.setenv("TOOROW_CANONICAL_IDENTITY_ENABLED", "1")
    monkeypatch.setattr(
        admin_api,
        "_check_canonical_principal",
        AsyncMock(return_value=(True, _principal())),
    )
    cursor = _install_db(monkeypatch, (False, True))

    ready = asyncio.run(admin_api._get_entry_state(_request()))

    assert json.loads(ready.body)["state"] == "hosted_entry_ready"
    assert cursor.execute.call_args.args[1] == ("person_1", "person_1")

    cursor.fetchone.return_value = (False, False)
    uninvited = asyncio.run(admin_api._get_entry_state(_request()))
    assert json.loads(uninvited.body)["state"] == "invitation_required"


def test_self_hosted_entry_state_requires_setup_until_claimed(monkeypatch):
    from core import admin_api

    monkeypatch.setenv("TOOROW_DEPLOYMENT_MODE", "self_hosted")
    monkeypatch.setenv("TOOROW_AUTH_MODE", "static")
    monkeypatch.setenv("TOOROW_CANONICAL_IDENTITY_ENABLED", "1")
    monkeypatch.setattr(
        admin_api,
        "_check_canonical_principal",
        AsyncMock(return_value=(True, _principal())),
    )
    cursor = _install_db(monkeypatch, (False, False))

    setup = asyncio.run(admin_api._get_entry_state(_request()))
    assert json.loads(setup.body)["state"] == "setup_required"
    assert cursor.execute.call_args.args[1] == ("person_1", "person_1")

    cursor.fetchone.return_value = (True, True)
    claimed = asyncio.run(admin_api._get_entry_state(_request()))
    assert json.loads(claimed.body)["state"] == "scoped"

    cursor.fetchone.return_value = (True, False)
    uninvited = asyncio.run(admin_api._get_entry_state(_request()))
    assert json.loads(uninvited.body)["state"] == "invitation_required"


def test_legacy_identity_mode_preserves_existing_scope_and_blocks_new_setup(monkeypatch):
    from core import admin_api, api_auth

    monkeypatch.setenv("TOOROW_DEPLOYMENT_MODE", "self_hosted")
    monkeypatch.setenv("TOOROW_AUTH_MODE", "static")
    monkeypatch.setenv("TOOROW_CANONICAL_IDENTITY_ENABLED", "0")
    monkeypatch.setattr(
        api_auth,
        "authenticate_api_request",
        AsyncMock(return_value=(True, "legacy-subject")),
    )
    canonical = AsyncMock()
    monkeypatch.setattr(admin_api, "_check_canonical_principal", canonical)
    cursor = _install_db(monkeypatch, (True,))

    existing = asyncio.run(admin_api._get_entry_state(_request()))

    assert json.loads(existing.body)["state"] == "scoped"
    assert cursor.execute.call_args.args[1] == ("legacy-subject",)
    canonical.assert_not_awaited()

    cursor.fetchone.return_value = (False,)
    fresh = asyncio.run(admin_api._get_entry_state(_request()))
    assert json.loads(fresh.body)["state"] == "identity_activation_required"


def test_disabled_auth_hosted_mode_preserves_local_first_scope(monkeypatch):
    from core import admin_api

    monkeypatch.setenv("TOOROW_DEPLOYMENT_MODE", "hosted")
    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    canonical = AsyncMock()
    monkeypatch.setattr(admin_api, "_check_canonical_principal", canonical)
    cursor = _install_db(monkeypatch, (False,))

    first_scope = asyncio.run(admin_api._get_entry_state(_request()))

    assert json.loads(first_scope.body)["state"] == "local_entry_ready"
    canonical.assert_not_awaited()

    cursor.fetchone.return_value = (True,)
    scoped = asyncio.run(admin_api._get_entry_state(_request()))
    assert json.loads(scoped.body)["state"] == "scoped"


def test_entry_state_route_is_registered():
    from core.admin_api import router

    paths = {route.path for route in router.routes if "GET" in route.methods}
    assert "/api/entry-state" in paths
