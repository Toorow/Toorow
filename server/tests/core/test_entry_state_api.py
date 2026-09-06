from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock

from core import entry_api  # AD-43 : le handler vit chez son sujet
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
    monkeypatch.setattr(
        admin_api,
        "_check_canonical_principal",
        AsyncMock(return_value=(True, _principal())),
    )
    cursor = _install_db(monkeypatch, (False, True))

    ready = asyncio.run(entry_api._get_entry_state(_request()))

    assert json.loads(ready.body)["state"] == "hosted_entry_ready"
    assert cursor.execute.call_args.args[1] == ("person_1", "person_1")

    cursor.fetchone.return_value = (False, False)
    uninvited = asyncio.run(entry_api._get_entry_state(_request()))
    assert json.loads(uninvited.body)["state"] == "invitation_required"


def test_self_hosted_entry_state_requires_setup_until_claimed(monkeypatch):
    from core import admin_api

    monkeypatch.setenv("TOOROW_DEPLOYMENT_MODE", "self_hosted")
    monkeypatch.setenv("TOOROW_AUTH_MODE", "static")
    monkeypatch.setattr(
        admin_api,
        "_check_canonical_principal",
        AsyncMock(return_value=(True, _principal())),
    )
    cursor = _install_db(monkeypatch, (False, False))

    setup = asyncio.run(entry_api._get_entry_state(_request()))
    assert json.loads(setup.body)["state"] == "setup_required"
    assert cursor.execute.call_args.args[1] == ("person_1", "person_1")

    cursor.fetchone.return_value = (True, True)
    claimed = asyncio.run(entry_api._get_entry_state(_request()))
    assert json.loads(claimed.body)["state"] == "scoped"

    cursor.fetchone.return_value = (True, False)
    uninvited = asyncio.run(entry_api._get_entry_state(_request()))
    assert json.loads(uninvited.body)["state"] == "invitation_required"


def test_entry_state_has_no_second_identity_and_never_reads_a_raw_subject(monkeypatch):
    """REPLACES `test_legacy_identity_mode_preserves_existing_scope_and_blocks_new_setup`.

    That test proved the branch removed on 2026-08-24: with the identity flag
    off -- its DEFAULT -- this route authenticated through
    `authenticate_api_request`, looked `app.org_members` up on the RAW OIDC
    subject, and answered `scoped` when it matched. It was a true proof of a real
    path, so it is replaced rather than deleted: what it proved must now be
    proved IMPOSSIBLE.

    Two assertions, one per half of what disappeared. The canonical resolver is
    the only door -- a caller it refuses gets 401, never a second lookup -- and
    no query is issued on an identity nothing resolved.
    """
    from core import admin_api, api_auth

    monkeypatch.setenv("TOOROW_DEPLOYMENT_MODE", "self_hosted")
    monkeypatch.setenv("TOOROW_AUTH_MODE", "static")
    legacy = AsyncMock(return_value=(True, "legacy-subject"))
    monkeypatch.setattr(api_auth, "authenticate_api_request", legacy)
    monkeypatch.setattr(
        admin_api, "_check_canonical_principal", AsyncMock(return_value=(False, None))
    )
    cursor = _install_db(monkeypatch, (True,))

    refused = asyncio.run(entry_api._get_entry_state(_request()))

    assert refused.status_code == 401
    assert json.loads(refused.body)["code"] == "unauthorized"
    legacy.assert_not_awaited()
    cursor.execute.assert_not_called()


def test_disabled_auth_hosted_mode_preserves_local_first_scope(monkeypatch):
    from core import admin_api

    monkeypatch.setenv("TOOROW_DEPLOYMENT_MODE", "hosted")
    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    canonical = AsyncMock()
    monkeypatch.setattr(admin_api, "_check_canonical_principal", canonical)
    cursor = _install_db(monkeypatch, (False,))

    first_scope = asyncio.run(entry_api._get_entry_state(_request()))

    assert json.loads(first_scope.body)["state"] == "local_entry_ready"
    canonical.assert_not_awaited()

    cursor.fetchone.return_value = (True,)
    scoped = asyncio.run(entry_api._get_entry_state(_request()))
    assert json.loads(scoped.body)["state"] == "scoped"


def test_entry_state_route_is_registered():
    from core.admin_api import router

    paths = {route.path for route in router.routes if "GET" in route.methods}
    assert "/api/entry-state" in paths
