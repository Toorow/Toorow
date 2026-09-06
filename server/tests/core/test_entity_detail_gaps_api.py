"""The HTTP door of the entity-gap inventory, which nothing exercised.

WHAT WAS AND WAS NOT COVERED. `test_entity_detail_gaps.py` pins the honesty of
the ANSWER -- `unavailable` is never `0 missing`, a capped scan says it was
capped, the gesture names an event stream. All of it on `inventory()` directly.
The route around it was reached by no test at all, and the route is where four
distinct answers a screen must tell apart are decided:

  * **401** with no identity -- no answer, not an empty one;
  * **400** when the caller names no Semantic View version or no dimension. This
    one is load-bearing: without it the handler would run a query with two empty
    strings and answer **404**, which reads as "that dimension does not exist"
    for what is really "you did not say which one";
  * **404** when the member is not bound to that view version -- and the same
    404 for a member of another Project, because a 403 would confirm it exists
    (lesson F-3 of story 27.2);
  * **200 with `state: unavailable`** when the physical plan cannot be resolved.
    The route builds that payload ITSELF rather than calling `inventory`, so
    every honest-absence key it carries is a second copy of the contract the
    other test file holds -- and a second copy is exactly what drifts.

No database: the value here is the branching, and a live Postgres would prove
none of it better.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

IDENTITY = "owner@example.com"
PROJECT = "proj_EXAMPLE"
VIEW_VERSION = "svv_EXAMPLE0000000000000000"
MEMBER = "sc_EXAMPLE00000000000000000"
BASE = f"/api/projects/{PROJECT}/analyze/entity-gaps"


class _Cursor:
    """Answers the one statement this route runs itself."""

    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        assert "app.semantic_view_version_bindings" in sql, f"unexpected statement: {sql}"

    def fetchall(self):
        return list(self._rows)


class _Connection:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _Cursor(self._rows)


def _client():
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    return TestClient(build_asgi_app(), raise_server_exceptions=False)


def _serving(rows=((("video"),),), plan=None, inventory_answer=None):
    @contextmanager
    def _fake_connection():
        yield _Connection(rows)

    patches = [
        patch("core.db.get_connection", _fake_connection),
        patch("core.admin_api._require_datastream_role", return_value=None),
        patch(
            "core.query_execution.resolve_physical_plan",
            return_value=plan if plan is not None else {"relation": "x"},
        ),
    ]
    if inventory_answer is not None:
        # `entity_detail_gaps_api` binds `inventory` at import time, so the name
        # to replace is the one IN the route module -- patching the source module
        # would leave the already-bound reference untouched and prove nothing.
        patches.append(
            patch("core.entity_detail_gaps_api.inventory", return_value=inventory_answer)
        )
    return patches


def _get(query: str = "", **kwargs):
    patches = _serving(**kwargs)
    auth = patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, IDENTITY)))
    with auth:
        for entered in patches:
            entered.start()
        try:
            with _client() as client:
                return client.get(f"{BASE}{query}")
        finally:
            for entered in reversed(patches):
                entered.stop()


_FULL_QUERY = f"?semantic_view_version_id={VIEW_VERSION}&member_id={MEMBER}"


def test_the_route_is_mounted_at_the_address_the_screen_asks_for():
    from core.admin_api import router

    paths = {getattr(route, "path", "") for route in router.routes}
    assert "/api/projects/{project_id}/analyze/entity-gaps" in paths


def test_no_authentication_is_no_answer():
    with patch("core.admin_api._check_auth", new=AsyncMock(return_value=(False, ""))):
        with _client() as client:
            response = client.get(f"{BASE}{_FULL_QUERY}")
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_naming_no_dimension_is_a_bad_request_and_never_a_missing_dimension():
    """400, not 404, and the difference is what a person reads.

    Without this branch the handler would query with two empty strings, find no
    binding and answer 404 -- "that dimension does not exist" for what is really
    "you did not say which one". Two different repairs.
    """
    response = _get(f"?semantic_view_version_id={VIEW_VERSION}")
    assert response.status_code == 400
    assert response.json()["code"] == "missing_field"
    # And the message names what to supply, not what went wrong internally.
    assert "Semantic View version" in response.json()["message"]


def test_naming_no_view_version_is_refused_the_same_way():
    response = _get(f"?member_id={MEMBER}")
    assert response.status_code == 400
    assert response.json()["code"] == "missing_field"


def test_a_member_that_is_not_bound_to_this_view_version_is_not_found():
    """404 and never 403: a 403 would confirm the object exists elsewhere."""
    response = _get(_FULL_QUERY, rows=())
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_an_ambiguous_binding_is_refused_rather_than_picked():
    """Two rows for one member is not a member to inventory -- it is a question."""
    response = _get(_FULL_QUERY, rows=(("video",), ("video",)))
    assert response.status_code == 404


def test_an_unresolvable_plan_answers_unavailable_and_never_zero_missing():
    """THE CONTRACT THIS ROUTE COPIES, held here so the copy cannot drift.

    `unavailable` and `0 missing` are opposite answers, and the second closes a
    case that is still open. Every count is `None`, never `0`, and the gesture
    says nothing is claimed.
    """
    response = _get(_FULL_QUERY, plan={"unavailable_reason": "no published relation"})
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "unavailable"
    assert body["unavailable_reason"] == "no published relation"
    assert body["observed_count"] is None
    assert body["detailed_count"] is None
    assert body["missing_count"] is None
    assert body["missing"] == []
    assert "nothing is claimed" in body["next_gesture"]
    # Evidence is never cached: a stale inventory is a wrong inventory.
    assert response.headers["cache-control"] == "no-store"


def test_a_resolvable_plan_returns_the_inventory_and_names_the_member():
    answer = {
        "member_id": MEMBER,
        "member_name": "video",
        "entity_kind": "video",
        "state": "measured",
        "observed_count": 519,
        "detailed_count": 3,
        "missing_count": 516,
        "missing": [],
        "missing_truncated": True,
        "observed_truncated": False,
        "next_gesture": "Collect the event stream that carries the identifier.",
    }
    response = _get(_FULL_QUERY, inventory_answer=answer)
    assert response.status_code == 200
    assert response.json() == answer
    assert response.headers["cache-control"] == "no-store"
