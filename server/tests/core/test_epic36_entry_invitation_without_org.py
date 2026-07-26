"""The ENTRY invitation: one invitation object, with or WITHOUT an organization.

Model (Jean, 2026-07-25):

    "invitation par le flow waitlist -> 0 Org"
    "invitation a join un workspace -> tu join un workspace"

It is a SINGLE invitation. Without ``org_id`` it is the entry invitation issued
after a waitlist approval: accepting it creates NO membership and NO grant -- the
person lands on "Welcome to toorow -> Create your organization" and creates their
own. With ``org_id`` nothing changes: they join THAT organization.

What these tests pin down
-------------------------
* issuance without an org records NULL, is platform-scoped, and grants nothing;
* only a PLATFORM admin (``TOOROW_SUPER_ADMINS``) may issue one -- anybody else
  gets a nondisclosing 404 and no invitation is minted;
* an invitation WITH an org keeps its org-membership gate untouched;
* acceptance without an org writes no ``org_members`` row, no ``resource_grants``
  row and no setup journey, and sends the person to create their organization;
* the one-organization-per-person cap does not block the person who just
  accepted one (the nominal case);
* an entry invitation stays visible: the listing scope is IS NOT DISTINCT FROM,
  never ``= NULL``.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.requests import Request


def _request(path: str, body: dict, *, path_params=None, headers=None) -> Request:
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": headers if headers is not None else [(b"idempotency-key", b"invite-1")],
            "path_params": path_params or {},
        },
        receive,
    )


def _conn(*rows):
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    if rows:
        cur.fetchone.side_effect = list(rows)
    cur.rowcount = 1
    conn.cursor.return_value = cur
    return conn, cur


# ---------------------------------------------------------------------------
# Issuance
# ---------------------------------------------------------------------------


def test_issue_without_org_is_platform_scoped_and_stores_no_org(monkeypatch):
    from core import invitations, operations

    monkeypatch.setenv("TOOROW_INVITATION_PEPPER", "p" * 32)
    monkeypatch.setenv("TOOROW_INVITATION_ORIGIN", "https://console.toorow.test")
    conn, cur = _conn()
    captured = {}

    def execute(operation_conn, spec, *, mutation):
        captured["spec"] = spec
        changed = mutation(operation_conn, "op-1")
        return operations.OperationResult(
            "op-1", "succeeded", changed.result, "audit-1", "outbox-1", False
        )

    monkeypatch.setattr(invitations, "execute_operation", execute)
    result = invitations.issue_invitation(
        conn,
        invited_identity="candidate@example.com",
        org_id=None,
        role="owner",
        project_grants=(),
        datastream_grants=(),
        issuer="platform-admin@toorow.test",
        expires_in_hours=48,
        policy_version="p1",
        idempotency_key="entry-invite-1",
        host_context={"host": "rest"},
        trace_id=None,
    )

    # No organization anywhere: not in the operation envelope, not in the row.
    assert captured["spec"].effective_org_id is None
    assert captured["spec"].resource_path[0] == "platform:invitations"
    insert = next(
        call for call in cur.execute.call_args_list if "INSERT INTO app.invitations" in call.args[0]
    )
    params = insert.args[1]
    assert params[2] is None  # org_id column
    assert json.loads(params[4]) == []  # grant_bindings
    assert result.delivery_url.startswith("https://console.toorow.test/invite#invite=")


def test_issue_without_org_requires_owner_result(monkeypatch):
    from core import invitations

    monkeypatch.setenv("TOOROW_INVITATION_PEPPER", "p" * 32)
    with pytest.raises(invitations.InvitationValidationError, match="owner authority"):
        invitations.issue_invitation(
            MagicMock(),
            invited_identity="candidate@example.com",
            org_id=None,
            role="member",
            project_grants=(),
            datastream_grants=(),
            issuer="platform-admin@toorow.test",
            expires_in_hours=48,
            policy_version="p1",
            idempotency_key="entry-invite-owner",
            host_context={"host": "rest"},
            trace_id=None,
        )

def test_issue_without_org_refuses_to_grant_anything(monkeypatch):
    from core import invitations

    monkeypatch.setenv("TOOROW_INVITATION_PEPPER", "p" * 32)
    with pytest.raises(invitations.InvitationValidationError, match="grants nothing"):
        invitations.issue_invitation(
            MagicMock(),
            invited_identity="candidate@example.com",
            org_id=None,
            role="owner",
            project_grants=(invitations.InvitationGrant("project", "proj-1", "view"),),
            datastream_grants=(),
            issuer="platform-admin@toorow.test",
            expires_in_hours=48,
            policy_version="p1",
            idempotency_key="entry-invite-2",
            host_context={"host": "rest"},
            trace_id=None,
        )


# ---------------------------------------------------------------------------
# Who may issue one
# ---------------------------------------------------------------------------


def _issue_api(monkeypatch, *, identity: str, super_admins: str):
    from core import admin_api, db, invitations, project_access

    conn, _ = _conn()

    @contextmanager
    def get_connection():
        yield conn

    monkeypatch.setenv("TOOROW_SUPER_ADMINS", super_admins)
    monkeypatch.setattr(db, "get_connection", get_connection)
    monkeypatch.setattr(db, "set_local_access_context", MagicMock())
    monkeypatch.setattr(admin_api, "_check_auth", AsyncMock(return_value=(True, identity)))
    monkeypatch.setattr(
        admin_api, "_check_invitation_identity", AsyncMock(return_value=(False, ""))
    )
    monkeypatch.setattr(admin_api, "write_audit_row", MagicMock())
    monkeypatch.setattr(project_access, "epic36_production_access_enabled", lambda: True)
    issue = MagicMock(
        return_value=invitations.InvitationIssueResult(
            invitation_id="invite-1",
            state="pending",
            expires_at="2026-08-01T00:00:00+00:00",
            operation_id="op-1",
            audit_event_id="audit-1",
            delivery_url="https://console.toorow.test/invite#invite=abc",
            replayed=False,
        )
    )
    monkeypatch.setattr(invitations, "issue_invitation", issue)
    response = asyncio.run(
        admin_api._issue_invitation(
            _request(
                "/api/invitations",
                {"invited_identity": "candidate@example.com", "role": "owner"},
            )
        )
    )
    return response, issue, conn


def test_platform_admin_may_issue_an_entry_invitation(monkeypatch):
    response, issue, conn = _issue_api(
        monkeypatch, identity="admin@toorow.test", super_admins="admin@toorow.test"
    )

    assert response.status_code == 201
    assert issue.call_args.kwargs["org_id"] is None
    assert json.loads(response.body)["delivery_handoff"]["url"].startswith("https://")
    conn.commit.assert_called_once()


def test_a_non_platform_admin_mints_no_entry_invitation(monkeypatch):
    response, issue, conn = _issue_api(
        monkeypatch, identity="somebody@example.com", super_admins="admin@toorow.test"
    )

    # 404, not 403: the surface is not revealed to a caller who is not allow-listed.
    assert response.status_code == 404
    issue.assert_not_called()
    conn.commit.assert_not_called()


def test_deny_by_default_when_the_allow_list_is_empty(monkeypatch):
    response, issue, _conn_ = _issue_api(
        monkeypatch, identity="admin@toorow.test", super_admins=""
    )
    assert response.status_code == 404
    issue.assert_not_called()


def test_an_org_scoped_invitation_still_requires_org_membership(monkeypatch):
    """Nothing is relaxed for the invitation that names an organization."""
    from core import admin_api, db, invitations, project_access
    from starlette.responses import JSONResponse

    conn, _ = _conn()

    @contextmanager
    def get_connection():
        yield conn

    # The caller IS a platform admin -- and that must not buy them anything here.
    monkeypatch.setenv("TOOROW_SUPER_ADMINS", "admin@toorow.test")
    monkeypatch.setattr(db, "get_connection", get_connection)
    monkeypatch.setattr(db, "set_local_access_context", MagicMock())
    monkeypatch.setattr(
        admin_api, "_check_auth", AsyncMock(return_value=(True, "admin@toorow.test"))
    )
    monkeypatch.setattr(project_access, "epic36_production_access_enabled", lambda: True)
    monkeypatch.setattr(
        admin_api,
        "_enforce_org_manage",
        lambda *_a: JSONResponse({"code": "forbidden"}, status_code=403),
    )
    issue = MagicMock()
    monkeypatch.setattr(invitations, "issue_invitation", issue)

    response = asyncio.run(
        admin_api._issue_invitation(
            _request(
                "/api/organizations/org-1/invitations",
                {"invited_identity": "user@example.com", "role": "member"},
                path_params={"org_id": "org-1"},
            )
        )
    )

    assert response.status_code == 403
    issue.assert_not_called()


# ---------------------------------------------------------------------------
# Acceptance
# ---------------------------------------------------------------------------


def _acceptance_row(identity_hash: str, org_id):
    return (
        "ixs-1",
        "invite-1",
        identity_hash,
        datetime.now(timezone.utc) + timedelta(minutes=5),
        None,
        None,
        org_id,
        "owner",
        [],
        "pending",
        datetime.now(timezone.utc) + timedelta(hours=1),
        None,
        "policy-v1",
        None,
        "platform-admin@toorow.test",
    )


def test_acceptance_without_org_creates_no_membership_and_no_org(monkeypatch):
    from core import invitations, operations, setup_responsibilities

    monkeypatch.setenv("TOOROW_INVITATION_PEPPER", "p" * 32)
    bootstrap = MagicMock()
    monkeypatch.setattr(setup_responsibilities, "bootstrap_journey_from_acceptance", bootstrap)
    identity_hash = invitations.prepare_identity_binding("user@example.com").identity_hash
    conn, cur = _conn(_acceptance_row(identity_hash, None))

    def execute(operation_conn, spec, *, mutation):
        assert spec.effective_org_id is None
        assert spec.resource_path[0] == "platform:invitations"
        changed = mutation(operation_conn, "op-accept")
        return operations.OperationResult(
            "op-accept", "succeeded", changed.result, "audit-1", "outbox-1", False
        )

    monkeypatch.setattr(invitations, "execute_operation", execute)
    result = invitations.accept_invitation(
        conn,
        session_value="s" * 48,
        verified_identity="user@example.com",
        confirmed=True,
        idempotency_key="accept-entry-1",
        host_context={"host": "rest"},
        trace_id=None,
    )

    sql = " ".join(call.args[0] for call in cur.execute.call_args_list)
    assert "INSERT INTO app.org_members" not in sql
    assert "INSERT INTO app.resource_grants" not in sql
    assert "INSERT INTO app.organizations" not in sql
    assert "SET state = 'accepted'" in sql
    bootstrap.assert_not_called()
    assert result.org_id is None
    assert result.explicit_none is True
    # The console root: signing in with no org lands on "Create your organization".
    assert result.next_url == invitations.CREATE_ORGANIZATION_NEXT_URL


def test_acceptance_with_org_still_materializes_the_membership(monkeypatch):
    from core import invitations, operations, setup_responsibilities

    monkeypatch.setenv("TOOROW_INVITATION_PEPPER", "p" * 32)
    monkeypatch.setattr(
        setup_responsibilities, "bootstrap_journey_from_acceptance", lambda *_a, **_k: "setup-1"
    )
    identity_hash = invitations.prepare_identity_binding("user@example.com").identity_hash
    conn, cur = _conn(
        _acceptance_row(identity_hash, "org-1"),
        (0,),  # resource_grants count
        None,  # no existing membership
        ("proj-org",),
    )
    monkeypatch.setattr(
        invitations,
        "execute_operation",
        lambda c, s, *, mutation: operations.OperationResult(
            "op-accept", "succeeded", mutation(c, "op-accept").result, "a", "o", False
        ),
    )

    result = invitations.accept_invitation(
        conn,
        session_value="s" * 48,
        verified_identity="user@example.com",
        confirmed=True,
        idempotency_key="accept-org-1",
        host_context={"host": "rest"},
        trace_id=None,
    )

    sql = " ".join(call.args[0] for call in cur.execute.call_args_list)
    assert "INSERT INTO app.org_members" in sql
    assert result.org_id == "org-1"
    assert result.next_url == "/p/proj-org/overview/getting-started"


def test_accept_api_reports_no_organization_without_inventing_one(monkeypatch):
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
            org_id=None,
            role="owner",
            explicit_grants=(),
            explicit_none=True,
            operation_id="op-1",
            audit_event_id="audit-1",
            outbox_event_id="outbox-1",
            next_url="/",
            replayed=False,
        ),
    )

    response = asyncio.run(
        admin_api._accept_invitation(
            Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/api/invitations/accept",
                    "headers": [
                        (b"idempotency-key", b"accept-1"),
                        (b"cookie", b"toorow_invitation_exchange=session-secret"),
                    ],
                },
                _receive({"confirmed": True}),
            )
        )
    )

    body = json.loads(response.body)
    assert body["organization_id"] is None
    assert body["next_url"] == "/"


def _receive(body: dict):
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}

    return receive


# ---------------------------------------------------------------------------
# The person may then create their organization
# ---------------------------------------------------------------------------


def _create_org_response(monkeypatch, membership_count):
    from core import admin_api

    monkeypatch.setenv("TOOROW_SUPER_ADMINS", "")
    monkeypatch.setattr(
        admin_api, "_check_auth", AsyncMock(return_value=(True, "newcomer@example.com"))
    )
    monkeypatch.setattr(
        admin_api,
        "_check_invitation_identity",
        AsyncMock(return_value=(True, "newcomer@example.com")),
    )
    monkeypatch.setattr(admin_api, "_count_active_memberships", lambda _keys: membership_count)
    return asyncio.run(
        admin_api._create_org(_request("/api/organizations", {"name": ""}, headers=[]))
    )


def test_the_one_org_cap_does_not_block_someone_who_accepted_an_entry_invitation(monkeypatch):
    """Accepting an entry invitation creates no membership -> the cap sees 0.

    The nominal case must go through. The response is a 422 on the empty name
    (the body is deliberately invalid): what matters is that it is NOT the 409
    ``organization_limit_reached`` -- the gate was passed.
    """
    response = _create_org_response(monkeypatch, 0)
    assert response.status_code == 422


def test_the_one_org_cap_still_refuses_a_second_organization(monkeypatch):
    response = _create_org_response(monkeypatch, 1)
    assert response.status_code == 409
    assert json.loads(response.body)["code"] == "organization_limit_reached"


# ---------------------------------------------------------------------------
# Nothing disappears from a view
# ---------------------------------------------------------------------------


def test_entry_invitations_are_a_listing_scope_of_their_own():
    from core.invitations import list_safe_invitations

    conn, cur = _conn()
    cur.fetchall.return_value = []
    list_safe_invitations(conn, org_id=None)
    sql, params = cur.execute.call_args.args
    assert "org_id IS NOT DISTINCT FROM %s" in sql
    assert params == (None,)


def test_entry_invitation_routes_are_registered():
    from core.admin_api import router

    routes = {(route.path, tuple(sorted(route.methods or ()))) for route in router.routes}
    paths = {path for path, _ in routes}
    assert "/api/invitations" in paths
    assert "/api/invitations/{invitation_id}/revoke" in paths
    assert "/api/invitations/{invitation_id}/resend" in paths
    # The static exchange/accept routes still exist and are declared first.
    declared = [route.path for route in router.routes]
    assert declared.index("/api/invitations/exchange") < declared.index("/api/invitations")
    assert declared.index("/api/invitations/accept") < declared.index("/api/invitations")


# ---------------------------------------------------------------------------
# The migration and the operations envelope it had to unblock
# ---------------------------------------------------------------------------


def test_migration_109_makes_the_org_optional_without_inventing_one():
    sql = Path("infra/nango/migrations/109_entry_invitation_without_org.sql").read_text(
        encoding="utf-8"
    )
    assert "ALTER TABLE app.invitations ALTER COLUMN org_id DROP NOT NULL" in sql
    assert "ALTER TABLE app.operations ALTER COLUMN effective_org_id DROP NOT NULL" in sql
    assert "operations_platform_idempotency" in sql
    assert "ck_invitation_grants_require_org" in sql
    # No ghost organization is created anywhere.
    assert "INSERT INTO app.organizations" not in sql


def test_platform_scope_operations_keep_their_replay_protection():
    from core import operations

    conn, cur = _conn()
    cur.fetchone.return_value = None
    prepared = operations.prepare_operation(
        operations.OperationSpec(
            command_type="invitation.issue",
            actor="admin@toorow.test",
            effective_org_id=None,
            resource_path=("platform:invitations", "invitation-subject:x"),
            idempotency_key="k-1",
            host_context={"host": "rest"},
            versions={"policy": "v1"},
            request_payload={},
            provider_references={},
            confirmation_mode="server",
            confirmation_reference=None,
            trace_id=None,
        )
    )
    with conn.cursor() as cursor:
        operations._existing_operation(cursor, prepared)
    sql = cur.execute.call_args.args[0]
    # `= NULL` is never true: the lookup must be NULL-safe or every platform-scope
    # replay would be re-executed instead of replayed.
    assert "effective_org_id IS NOT DISTINCT FROM %s" in sql


def test_a_blank_org_is_still_rejected():
    from core import operations

    with pytest.raises(operations.OperationValidationError, match="effective_org_id"):
        operations.prepare_operation(
            operations.OperationSpec(
                command_type="invitation.issue",
                actor="a",
                effective_org_id="   ",
                resource_path=("x",),
                idempotency_key="k",
                host_context={},
                versions={},
                request_payload={},
                provider_references={},
                confirmation_mode="server",
                confirmation_reference=None,
                trace_id=None,
            )
        )
