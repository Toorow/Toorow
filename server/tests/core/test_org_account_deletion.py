"""Offline unit tests for the RGPD deletion path (organization + account).

Covers what a human actually depends on before pressing "delete":
  * GET /api/organizations/{id}/deletion-preview announces the real composition;
  * DELETE /api/organizations/{id} refuses without the confirmation header and
    refuses for a non-manager;
  * the no-partial-deletion invariant: if the warehouse drop cannot be
    confirmed, NOTHING is committed on the Postgres side;
  * GET /api/me/deletion-preview names the organizations that depend on the
    account, and DELETE /api/me refuses (409) when the caller is the last owner
    of an org that still has other active members;
  * what a successful account erasure erases -- and what it deliberately keeps.

No real DB: every connection is a routed mock (fetch answers depend on the last
executed SQL), so the assertions are about the FLOW, not about psycopg.
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

_AUTH = "core.admin_api._check_auth"
_IDENTITY = "user@example.com"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _RoutedCursor:
    """Cursor whose fetch* answer depends on the LAST SQL it was given.

    Keys are substrings of the statement (``"FROM app.projects"``); the first
    match wins, so a test only declares the queries it cares about and every
    other probe degrades to "nothing found" -- exactly what the endpoint must
    tolerate on a deployment where a table is missing.
    """

    def __init__(self, one=None, many=None, rowcounts=None):
        self._one = one or {}
        self._many = many or {}
        self._rowcounts = rowcounts or {}
        self._sql = ""
        self.executed: list[str] = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def execute(self, sql, params=None):  # noqa: ARG002 -- params unused by the double
        self._sql = " ".join(str(sql).split())
        self.executed.append(self._sql)
        self.rowcount = self._match(self._rowcounts, default=0)

    def _match(self, mapping, default=None):
        for key, value in mapping.items():
            if " ".join(key.split()) in self._sql:
                return value
        return default

    def fetchone(self):
        return self._match(self._one)

    def fetchall(self):
        return self._match(self._many, default=[])


def _conn(cursor) -> MagicMock:
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn


def _cm(conn) -> MagicMock:
    cm = MagicMock()
    cm.__enter__.return_value = conn
    cm.__exit__.return_value = False
    return cm


def _request(path_params=None, headers=None) -> MagicMock:
    req = MagicMock()
    req.path_params = path_params or {}
    req.headers = headers or {}
    req.body = AsyncMock(return_value=b"")
    return req


class _Schemas:
    """Stand-in for warehouse_tenancy.OrgSchemas (only .raw/.marts are read)."""

    raw = "org_acme_raw"
    marts = "org_acme_marts"


# ---------------------------------------------------------------------------
# GET /api/organizations/{org_id}/deletion-preview
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_org_preview_lists_composition_and_datasets():
    """The preview announces projects, per-table counts and the warehouse datasets."""
    from core.admin_api import _org_deletion_preview

    cur = _RoutedCursor(
        one={
            "SELECT id, name, slug FROM app.organizations": (
                "org_01",
                "Acme",
                "acme",
            ),
            "FROM app.datastreams": (3,),
            "FROM app.connection_ref": (2,),
            "FROM app.invitations": (1,),
            "FROM app.org_members": (4,),
            "FROM app.operations": (7,),
        },
        many={
            "FROM app.projects": [
                ("proj_01", "Retail", "archived"),
                ("proj_02", "Brand", "archived"),
            ]
        },
    )

    with (
        patch(_AUTH, return_value=(True, _IDENTITY)),
        patch("core.db.get_connection", return_value=_cm(_conn(cur))),
        patch("core.admin_api._enforce_org_manage", return_value=None),
        patch("core.warehouse_tenancy.resolve_org_schemas", return_value=_Schemas()),
    ):
        resp = await _org_deletion_preview(_request({"org_id": "org_01"}))

    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["org_id"] == "org_01"
    assert body["name"] == "Acme"
    assert body["slug"] == "acme"
    assert [p["id"] for p in body["projects"]] == ["proj_01", "proj_02"]
    assert body["counts"] == {
        "datastreams": 3,
        "connections": 2,
        "invitations": 1,
        "members": 4,
        "operations": 7,
    }
    assert body["warehouse_datasets"] == ["org_acme_raw", "org_acme_marts"]
    # Everything archived and the warehouse resolves -> nothing blocks.
    assert body["blockers"] == []


@pytest.mark.anyio
async def test_org_preview_reports_active_projects_as_blocker():
    """An active project is announced as a blocker, not discovered on the 409."""
    from core.admin_api import _org_deletion_preview

    cur = _RoutedCursor(
        one={"SELECT id, name, slug FROM app.organizations": ("org_01", "Acme", "acme")},
        many={"FROM app.projects": [("proj_01", "Retail", "active")]},
    )

    with (
        patch(_AUTH, return_value=(True, _IDENTITY)),
        patch("core.db.get_connection", return_value=_cm(_conn(cur))),
        patch("core.admin_api._enforce_org_manage", return_value=None),
        patch("core.warehouse_tenancy.resolve_org_schemas", return_value=_Schemas()),
    ):
        resp = await _org_deletion_preview(_request({"org_id": "org_01"}))

    body = json.loads(resp.body)
    kinds = [b["kind"] for b in body["blockers"]]
    assert kinds == ["active_projects"]
    assert "Retail" in body["blockers"][0]["detail"]


@pytest.mark.anyio
async def test_org_preview_unknown_org_is_404():
    from core.admin_api import _org_deletion_preview

    cur = _RoutedCursor(one={})
    with (
        patch(_AUTH, return_value=(True, _IDENTITY)),
        patch("core.db.get_connection", return_value=_cm(_conn(cur))),
    ):
        resp = await _org_deletion_preview(_request({"org_id": "org_missing"}))
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_org_preview_requires_manager():
    """A non-manager cannot even read what an org is made of."""
    from core.admin_api import _org_deletion_preview
    from starlette.responses import JSONResponse

    cur = _RoutedCursor(
        one={"SELECT id, name, slug FROM app.organizations": ("org_01", "Acme", "acme")}
    )
    forbidden = JSONResponse({"code": "forbidden", "message": "no"}, status_code=403)

    with (
        patch(_AUTH, return_value=(True, "intruder@example.com")),
        patch("core.db.get_connection", return_value=_cm(_conn(cur))),
        patch("core.admin_api._enforce_org_manage", return_value=forbidden),
    ):
        resp = await _org_deletion_preview(_request({"org_id": "org_01"}))
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# DELETE /api/organizations/{org_id}
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_delete_org_refused_for_non_manager():
    """Story 21.5 gate: a non owner/admin gets 403 and nothing is committed."""
    from core.admin_api import _delete_org
    from starlette.responses import JSONResponse

    cur = _RoutedCursor(
        one={"SELECT id, name, slug FROM app.organizations": ("org_01", "Acme", "acme")}
    )
    conn = _conn(cur)
    forbidden = JSONResponse({"code": "forbidden", "message": "no"}, status_code=403)

    with (
        patch(_AUTH, return_value=(True, "intruder@example.com")),
        patch("core.db.get_connection", return_value=_cm(conn)),
        patch("core.admin_api._enforce_org_manage", return_value=forbidden),
    ):
        resp = await _delete_org(
            _request({"org_id": "org_01"}, {"X-Confirm-Delete": "drop-warehouse-data"})
        )

    assert resp.status_code == 403
    conn.commit.assert_not_called()


@pytest.mark.anyio
async def test_delete_org_reports_what_was_removed():
    """200 carries `removed`, sourced from the same facts the preview shows."""
    from core.admin_api import _delete_org

    cur = _RoutedCursor(
        one={
            "SELECT id, name, slug FROM app.organizations": ("org_01", "Acme", "acme"),
            "FROM app.projects WHERE org_id = %s AND status != 'archived'": None,
            "FROM app.datastreams": (3,),
            "FROM app.connection_ref": (2,),
            "FROM app.invitations": (0,),
            "FROM app.org_members": (1,),
            "FROM app.operations": (5,),
        },
        many={
            "SELECT id, name, status FROM app.projects": [
                ("proj_01", "Retail", "archived")
            ],
            "FROM app.dataset_access_grants": [],
        },
    )
    conn = _conn(cur)

    with (
        patch(_AUTH, return_value=(True, _IDENTITY)),
        patch("core.db.get_connection", return_value=_cm(conn)),
        patch("core.admin_api._enforce_org_manage", return_value=None),
        patch("core.warehouse_tenancy.resolve_org_schemas", return_value=_Schemas()),
        patch(
            "core.org_purge.purge_org_tree",
            return_value={"total_rows": 42, "rows_by_table": {"app.datastreams": 3}},
        ),
        patch(
            "core.warehouse_tenancy.drop_org_schemas",
            return_value={"status": "ok", "raw": "org_acme_raw", "marts": "org_acme_marts"},
        ),
        patch("core.audit.insert_audit_row", return_value="aud_1"),
    ):
        resp = await _delete_org(
            _request({"org_id": "org_01"}, {"X-Confirm-Delete": "drop-warehouse-data"})
        )

    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["deleted"] is True
    assert body["removed"]["projects"] == 1
    assert body["removed"]["datastreams"] == 3
    assert body["removed"]["connections"] == 2
    assert body["removed"]["operations"] == 5
    assert body["removed"]["tenant_rows"] == 42
    conn.commit.assert_called_once()


@pytest.mark.anyio
async def test_delete_org_no_partial_deletion_when_warehouse_drop_fails():
    """RGPD invariant: an unconfirmable warehouse drop rolls back the whole thing."""
    from core.admin_api import _delete_org

    cur = _RoutedCursor(
        one={
            "SELECT id, name, slug FROM app.organizations": ("org_01", "Acme", "acme"),
            "FROM app.projects WHERE org_id = %s AND status != 'archived'": None,
        },
        many={"FROM app.dataset_access_grants": []},
    )
    conn = _conn(cur)

    with (
        patch(_AUTH, return_value=(True, _IDENTITY)),
        patch("core.db.get_connection", return_value=_cm(conn)),
        patch("core.admin_api._enforce_org_manage", return_value=None),
        patch("core.warehouse_tenancy.resolve_org_schemas", return_value=_Schemas()),
        patch(
            "core.org_purge.purge_org_tree",
            return_value={"total_rows": 5, "rows_by_table": {}},
        ),
        patch(
            "core.warehouse_tenancy.drop_org_schemas",
            side_effect=RuntimeError("bigquery unreachable"),
        ),
    ):
        resp = await _delete_org(
            _request({"org_id": "org_01"}, {"X-Confirm-Delete": "drop-warehouse-data"})
        )

    assert resp.status_code == 500
    assert json.loads(resp.body)["code"] == "schema_drop_failed"
    conn.rollback.assert_called()
    conn.commit.assert_not_called()


# ---------------------------------------------------------------------------
# GET /api/me/deletion-preview
# ---------------------------------------------------------------------------


def _membership_row(
    org_id="org_01",
    name="Acme",
    slug="acme",
    role="owner",
    status="active",
    others=0,
    other_owners=0,
):
    return (org_id, name, slug, role, status, others, other_owners)


@pytest.mark.anyio
async def test_me_preview_flags_sole_ownership_with_members():
    """The account preview names the org that would be orphaned, and why."""
    from core.admin_api import _get_my_deletion_preview

    cur = _RoutedCursor(
        one={"FROM app.user_profiles": (_IDENTITY, "Jean", "jean@example.com", None, None, None)},
        many={
            "FROM app.org_members m": [
                _membership_row(others=2, other_owners=0),
                _membership_row(org_id="org_02", name="Beta", role="member", others=5),
            ]
        },
    )

    with (
        patch(_AUTH, return_value=(True, _IDENTITY)),
        patch("core.db.get_connection", return_value=_cm(_conn(cur))),
    ):
        resp = await _get_my_deletion_preview(_request())

    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["identity"] == _IDENTITY
    assert body["email"] == "jean@example.com"
    assert [m["org_id"] for m in body["memberships"]] == ["org_01", "org_02"]
    assert body["memberships"][0]["other_active_members"] == 2
    # Sole owner of Acme -> listed; Beta is a plain membership -> not listed.
    assert body["sole_owner_of"] == [{"org_id": "org_01", "org_name": "Acme"}]
    assert [b["kind"] for b in body["blockers"]] == ["sole_owner_with_members"]
    assert body["organizations_erased_with_account"] == []


@pytest.mark.anyio
async def test_me_preview_lists_org_that_leaves_with_the_account():
    """Sole owner, nobody else active -> the org is announced as leaving too."""
    from core.admin_api import _get_my_deletion_preview

    cur = _RoutedCursor(
        one={"FROM app.user_profiles": (_IDENTITY, None, None, None, None, None)},
        many={"FROM app.org_members m": [_membership_row(others=0)]},
    )

    facts = {
        "org_id": "org_01",
        "name": "Acme",
        "slug": "acme",
        "projects": [],
        "counts": {},
        "warehouse_datasets": ["org_acme_raw", "org_acme_marts"],
        "blockers": [],
    }
    with (
        patch(_AUTH, return_value=(True, _IDENTITY)),
        patch("core.db.get_connection", return_value=_cm(_conn(cur))),
        patch("core.admin_api._org_deletion_facts", return_value=facts),
    ):
        resp = await _get_my_deletion_preview(_request())

    body = json.loads(resp.body)
    assert body["blockers"] == []
    assert body["organizations_erased_with_account"] == [
        {"org_id": "org_01", "name": "Acme", "slug": "acme"}
    ]


# ---------------------------------------------------------------------------
# DELETE /api/me
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_delete_me_requires_confirmation_header():
    from core.admin_api import _delete_me

    with patch(_AUTH, return_value=(True, _IDENTITY)):
        resp = await _delete_me(_request())
    assert resp.status_code == 422
    assert json.loads(resp.body)["code"] == "confirmation_required"


@pytest.mark.anyio
async def test_delete_me_refuses_sole_owner_with_active_members():
    """409 with the remedy spelled out -- no org is ever left ownerless."""
    from core.admin_api import _delete_me

    cur = _RoutedCursor(
        one={"FROM app.user_profiles": (_IDENTITY, None, None, None, None, None)},
        many={"FROM app.org_members m": [_membership_row(others=3)]},
    )
    conn = _conn(cur)

    with (
        patch(_AUTH, return_value=(True, _IDENTITY)),
        patch("core.db.get_connection", return_value=_cm(conn)),
    ):
        resp = await _delete_me(_request(headers={"X-Confirm-Delete": "erase-account"}))

    assert resp.status_code == 409
    body = json.loads(resp.body)
    assert body["code"] == "account_deletion_blocked"
    assert body["blockers"][0]["kind"] == "sole_owner_with_members"
    detail = body["blockers"][0]["detail"]
    assert "Transfer ownership" in detail and "Acme" in detail
    # Nothing was erased on the way to the refusal.
    conn.commit.assert_not_called()


@pytest.mark.anyio
async def test_delete_me_erases_memberships_and_states_what_is_retained():
    from core.admin_api import _delete_me

    cur = _RoutedCursor(
        one={"FROM app.audit_log": (12,)},
        rowcounts={
            "DELETE FROM app.org_members": 2,
            "DELETE FROM app.project_members": 3,
            "DELETE FROM app.user_profiles": 1,
        },
    )
    conn = _conn(cur)

    facts = {
        "identity": _IDENTITY,
        "email": "jean@example.com",
        "memberships": [],
        "sole_owner_of": [],
        "blockers": [],
        "organizations_erased_with_account": [],
    }
    with (
        patch(_AUTH, return_value=(True, _IDENTITY)),
        patch("core.db.get_connection", return_value=_cm(conn)),
        patch("core.admin_api._account_deletion_facts", return_value=facts),
        patch("core.audit.insert_audit_row", return_value="aud_1"),
    ):
        resp = await _delete_me(_request(headers={"X-Confirm-Delete": "erase-account"}))

    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["deleted"] is True
    assert body["erased"] == {
        "profile": True,
        "org_memberships": 2,
        "project_memberships": 3,
        "organizations": [],
    }
    # The honest half: audit entries survive, and the payload says why.
    assert body["retained"]["audit_entries"] == 12
    assert "audit" in body["retained"]["reason"].lower()
    conn.commit.assert_called_once()


@pytest.mark.anyio
async def test_delete_me_erases_the_orgs_that_belong_to_nobody_else():
    """A sole-member org leaves with the account -- warehouse datasets included."""
    from core.admin_api import _delete_me

    cur = _RoutedCursor(one={"FROM app.audit_log": (4,)})
    conn = _conn(cur)

    facts = {
        "identity": _IDENTITY,
        "email": None,
        "memberships": [],
        "sole_owner_of": [{"org_id": "org_01", "org_name": "Acme"}],
        "blockers": [],
        "organizations_erased_with_account": [
            {"org_id": "org_01", "name": "Acme", "slug": "acme"}
        ],
    }
    erase_result = {
        "removed": {"projects": 0, "datastreams": 1, "tenant_rows": 9},
        "drop_status": "ok",
        "warehouse_datasets": ["org_acme_raw", "org_acme_marts"],
    }
    with (
        patch(_AUTH, return_value=(True, _IDENTITY)),
        patch("core.db.get_connection", return_value=_cm(conn)),
        patch("core.admin_api._account_deletion_facts", return_value=facts),
        patch(
            "core.admin_api._erase_org_transactional",
            return_value=(erase_result, None),
        ) as erase,
        patch("core.audit.insert_audit_row", return_value="aud_1"),
    ):
        resp = await _delete_me(_request(headers={"X-Confirm-Delete": "erase-account"}))

    assert resp.status_code == 200
    body = json.loads(resp.body)
    erase.assert_called_once()
    assert body["erased"]["organizations"][0]["org_id"] == "org_01"
    assert body["erased"]["organizations"][0]["warehouse_datasets"] == [
        "org_acme_raw",
        "org_acme_marts",
    ]


@pytest.mark.anyio
async def test_delete_me_stops_when_an_org_erasure_is_refused():
    """A refused org erasure aborts the account erasure -- and says so."""
    from core.admin_api import _delete_me
    from starlette.responses import JSONResponse

    cur = _RoutedCursor()
    conn = _conn(cur)

    facts = {
        "identity": _IDENTITY,
        "email": None,
        "memberships": [],
        "sole_owner_of": [{"org_id": "org_01", "org_name": "Acme"}],
        "blockers": [],
        "organizations_erased_with_account": [
            {"org_id": "org_01", "name": "Acme", "slug": "acme"}
        ],
    }
    refusal = JSONResponse(
        {"code": "org_has_active_projects", "message": "archive first"},
        status_code=409,
    )
    with (
        patch(_AUTH, return_value=(True, _IDENTITY)),
        patch("core.db.get_connection", return_value=_cm(conn)),
        patch("core.admin_api._account_deletion_facts", return_value=facts),
        patch(
            "core.admin_api._erase_org_transactional", return_value=(None, refusal)
        ),
    ):
        resp = await _delete_me(_request(headers={"X-Confirm-Delete": "erase-account"}))

    assert resp.status_code == 409
    body = json.loads(resp.body)
    assert body["code"] == "org_erasure_failed"
    assert body["cause"]["code"] == "org_has_active_projects"
    # The account itself was never touched.
    conn.commit.assert_not_called()
