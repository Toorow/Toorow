"""Declaring a canonical field was a side effect of describing a FILE.

`canonical_field_registry.declare_many` had exactly one production caller,
`file_source_template_api.py:717`. A Project fed by a connector pull therefore had
no door: measured 2026-08-13 on the YouTube channel of the derivation dossier, the
visible vocabulary was the thirteen platform rows, of which zero name a video
measure, and a Skill binding `mdm_tags: [views]` was refused for naming an
ungoverned field. The amendment is in `governance.md`; this file holds the route
to what it says.

WHAT EACH TEST PINS IS AN INVARIANT OF THAT AMENDMENT, not a happy path:

  * the batch is ALL OR NOTHING -- a refused declaration commits nothing, because
    a half-described object validates against some of its own columns, which
    reads as "this file is partly wrong" rather than "I did not finish";
  * a WRITE asks more than a read -- a viewer who could mint would be writing the
    vocabulary every binding of the Project is validated against;
  * the door mints PROJECT fields and refuses the platform scope, which is the
    signature of `declare_project_field` and must not be softened by a route.
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

VIEWS = {
    "canonical_name": "views",
    "concept_kind": "metric",
    "value_type": "integer",
    "aggregation": "sum",
}
CHANNEL = {
    "canonical_name": "channel_id",
    "concept_kind": "dimension",
    "value_type": "string",
}


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


def _request(body) -> Request:
    payload = json.dumps(body).encode()
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/api/projects/{PROJECT}/mdm/canonical-fields",
            "headers": [(b"content-type", b"application/json")],
            "path_params": {"project_id": PROJECT},
        },
        receive,
    )


@pytest.fixture
def wired(monkeypatch):
    """Authenticated, a member, and a connection that counts its transactions."""
    import contextlib

    conn = FakeConnection()
    calls: dict = {"declared": None, "role": None}

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

    def _declare_many(_conn, *, project_id, fields, actor):
        calls["declared"] = list(fields)
        return [dict(field, id=f"mdm_{i}") for i, field in enumerate(fields)]

    monkeypatch.setattr("core.canonical_field_registry.declare_many", _declare_many)
    return conn, calls


@pytest.mark.anyio
async def test_a_connector_pull_project_can_declare_its_own_vocabulary(wired):
    conn, calls = wired

    response = await module._declare_canonical_fields(_request({"fields": [VIEWS, CHANNEL]}))

    assert response.status_code == 201
    assert [f["canonical_name"] for f in json.loads(response.body)["minted"]] == [
        "views",
        "channel_id",
    ]
    assert calls["declared"] == [VIEWS, CHANNEL]
    assert conn.committed == 1, "one batch, one commit"


@pytest.mark.anyio
async def test_a_refused_declaration_commits_nothing_of_the_batch(wired, monkeypatch):
    conn, _calls = wired
    from core.canonical_field_registry import CanonicalFieldError

    def _refuse(_conn, *, project_id, fields, actor):
        raise CanonicalFieldError("a dimension carries no aggregation -- it is not a measure")

    monkeypatch.setattr("core.canonical_field_registry.declare_many", _refuse)

    response = await module._declare_canonical_fields(
        _request({"fields": [VIEWS, dict(CHANNEL, aggregation="sum")]})
    )

    assert response.status_code == 422
    assert json.loads(response.body)["code"] == "invalid_declaration"
    assert conn.committed == 0, "a refused batch reached the vocabulary"
    assert conn.rolled_back == 1


@pytest.mark.anyio
async def test_minting_demands_the_member_role_not_merely_org_access(wired):
    _conn, calls = wired

    await module._declare_canonical_fields(_request({"fields": [VIEWS]}))

    assert calls["role"] == "member"


@pytest.mark.anyio
async def test_a_viewer_is_refused_by_the_role_gate(wired, monkeypatch):
    conn, _calls = wired
    monkeypatch.setattr(
        "core.admin_api._require_datastream_role",
        lambda *_a, **_kw: JSONResponse({"code": "forbidden"}, status_code=403),
    )

    response = await module._declare_canonical_fields(_request({"fields": [VIEWS]}))

    assert response.status_code == 403
    assert conn.committed == 0


@pytest.mark.anyio
@pytest.mark.parametrize("body", [{}, {"fields": []}, {"fields": "views"}, []])
async def test_an_empty_or_shapeless_batch_is_refused_before_any_write(wired, body):
    conn, _calls = wired

    response = await module._declare_canonical_fields(_request(body))

    assert response.status_code == 422
    assert conn.committed == 0


def test_the_route_is_declared_on_the_same_address_as_the_reader():
    """A door the GET reader cannot list would mint invisible vocabulary."""
    methods = {
        method
        for route in module.MDM_CANONICAL_FIELD_ROUTES
        if route.path == module._BASE
        for method in route.methods
        if method in {"GET", "POST"}
    }

    assert methods == {"GET", "POST"}
