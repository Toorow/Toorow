"""A canonical field could be declared and never retired (AI-304).

The project route served `POST` and `GET` and nothing else, so a name typed by
mistake stayed in the vocabulary for good and the only repair the product
offered was SQL by hand. Nothing had to be designed for this: migration 032 has
carried `CHECK (status IN ('active', 'archived'))` from the start, both unique
indexes exclude archived rows *"so a name can be retired and re-minted"*, and
`canonical_field_registry.archive_project_field` was written and audited under
`canonical_field.archived` -- with ZERO callers. What was missing is the door.

WHAT EACH TEST PINS IS AN INVARIANT OF THE AMENDMENT in `governance.md`:

  * retiring goes through the audited registry function, and commits once;
  * a write asks more than a read -- `member`, the same as the mint, because a
    viewer who could retire would be editing the vocabulary every binding of the
    Project is validated against;
  * an id that is not an active field of THIS Project answers 404 and nothing
    else, whether it never existed, was already retired, or belongs to another
    Project: telling the three apart would say whether an id exists elsewhere;
  * a refused retirement commits nothing.
"""

from __future__ import annotations

import json

import pytest
from core import mdm_canonical_fields_api as module
from starlette.requests import Request
from starlette.responses import JSONResponse

PROJECT = "proj_EXAMPLE"
ORG = "org_01EXAMPLE0000000000000"
IDENTITY = "person_01EXAMPLE00000000000000"
FIELD = "mdm_01EXAMPLE0000000000000000"


class FakeConnection:
    def __init__(self):
        self.committed = 0
        self.rolled_back = 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1


def _request(field_id: str = FIELD) -> Request:
    async def receive():
        return {"type": "http.disconnect"}

    return Request(
        {
            "type": "http",
            "method": "DELETE",
            "path": f"/api/projects/{PROJECT}/mdm/canonical-fields/{field_id}",
            "headers": [],
            "path_params": {"project_id": PROJECT, "field_id": field_id},
        },
        receive,
    )


@pytest.fixture
def wired(monkeypatch):
    """Authenticated, a member, and a connection that counts its transactions."""
    import contextlib

    conn = FakeConnection()
    calls: dict = {"archived": None, "role": None}

    async def _authenticated(_request):
        return True, IDENTITY

    monkeypatch.setattr(module, "_check_auth", _authenticated)
    monkeypatch.setattr(module, "_guard", lambda _p, _i: (ORG, None))

    @contextlib.contextmanager
    def _get_connection():
        yield conn

    monkeypatch.setattr("core.db.get_connection", _get_connection)

    def _role(project_id, identity, minimum_role, _conn, **_kw):
        calls["role"] = minimum_role
        return None

    monkeypatch.setattr("core.admin_api._require_datastream_role", _role)

    def _archive(_conn, *, project_id, field_id, actor):
        calls["archived"] = (project_id, field_id, actor)
        return {
            "id": field_id,
            "project_id": project_id,
            "canonical_name": "views",
            "status": "archived",
        }

    monkeypatch.setattr("core.canonical_field_registry.archive_project_field", _archive)
    return conn, calls


@pytest.mark.anyio
async def test_a_field_declared_by_mistake_can_leave_the_vocabulary(wired):
    conn, calls = wired

    response = await module._archive_canonical_field(_request())

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["canonical_field"] == {
        "id": FIELD,
        "canonical_name": "views",
        "status": "archived",
    }
    assert calls["archived"] == (PROJECT, FIELD, IDENTITY)
    assert conn.committed == 1


@pytest.mark.anyio
async def test_retiring_goes_through_the_audited_registry_function(wired, monkeypatch):
    """Not a hand-written UPDATE in the route.

    `archive_project_field` is the only writer that records the hand-over of the
    NAME -- the audit row carries `released_name`, and archiving is what frees
    it. A route that wrote the column itself would retire a field and lose the
    only trace that the name changed hands.
    """
    _conn, calls = wired

    await module._archive_canonical_field(_request())

    assert calls["archived"] is not None, "the registry function was bypassed"


@pytest.mark.anyio
async def test_retiring_demands_the_member_role_not_merely_org_access(wired):
    _conn, calls = wired

    await module._archive_canonical_field(_request())

    assert calls["role"] == "member"


@pytest.mark.anyio
async def test_a_viewer_is_refused_by_the_role_gate(wired, monkeypatch):
    conn, _calls = wired
    monkeypatch.setattr(
        "core.admin_api._require_datastream_role",
        lambda *_a, **_kw: JSONResponse({"code": "forbidden"}, status_code=403),
    )

    response = await module._archive_canonical_field(_request())

    assert response.status_code == 403
    assert conn.committed == 0


@pytest.mark.anyio
async def test_an_id_that_is_not_an_active_field_here_answers_404(wired, monkeypatch):
    """Never declared, already retired, and another Project's are ONE answer.

    `archive_project_field` matches on `project_id = %s AND status = 'active'`,
    so a platform row is not found here rather than refused here -- which is the
    same posture `declare_project_field` takes by refusing a null project.
    """
    conn, _calls = wired
    from core.canonical_field_registry import CanonicalFieldError

    def _refuse(_conn, *, project_id, field_id, actor):
        raise CanonicalFieldError("no active canonical field with that id in this project")

    monkeypatch.setattr("core.canonical_field_registry.archive_project_field", _refuse)

    response = await module._archive_canonical_field(_request("mdm_01NOTHERE000000000000000"))

    assert response.status_code == 404
    assert conn.committed == 0, "a refused retirement committed"
    assert conn.rolled_back == 1


@pytest.mark.anyio
async def test_an_unauthenticated_caller_gets_401_and_no_transaction(wired, monkeypatch):
    conn, _calls = wired

    async def _anonymous(_request):
        return False, ""

    monkeypatch.setattr(module, "_check_auth", _anonymous)

    response = await module._archive_canonical_field(_request())

    assert response.status_code == 401
    assert conn.committed == 0


@pytest.mark.anyio
async def test_the_route_is_mounted_on_the_single_field_address(wired):
    """The verb is the Common Keys sibling's -- one family, one gesture."""
    mounted = {
        (route.path, method)
        for route in module.MDM_CANONICAL_FIELD_ROUTES
        for method in route.methods
    }

    assert (
        "/api/projects/{project_id}/mdm/canonical-fields/{field_id}",
        "DELETE",
    ) in mounted
