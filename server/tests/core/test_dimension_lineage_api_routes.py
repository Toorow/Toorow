"""The console door onto a client label, judged at the ROUTE (2026-08-31).

WHY THIS FILE EXISTS. `governance.md` ratifies two doors onto one state -- the
console (`/api/dimension-lineage/*`) and the model (`set_dimension_label`). The
model door has a suite. The console door had NONE: measured 2026-08-31,
`grep -rn dimension_lineage_api server/tests` returned only prose citations, and
the guard every one of its four handlers calls first had never been exercised by
a request.

What that hid is the defect this file was written for. `_guard_org_and_project`
asked the MANAGE question before the MEMBERSHIP one, so a caller who named a
project of a neighbouring organization was told `403 Droits insuffisants` while a
caller who named an id nobody ever created was told `404 Project not found.`
Comparing two refusals therefore enumerated the Projects of the platform -- the
exact bullet of the "Incomplete if" list this surface owns: *"you may not", "it
does not exist" and "it could not be checked" wear three different envelopes*.

NO PROJECT OR ORGANIZATION NAME IS ASSERTED BELOW as a fact about the platform:
every id here is a fixture id, and what is compared is one refusal against
another.
"""

from __future__ import annotations

import contextlib
import json
import os
from unittest.mock import AsyncMock, patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import pytest  # noqa: E402
from core.dimension_lineage_api import DIMENSION_LINEAGE_ROUTES  # noqa: E402
from starlette.routing import Router  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

ORG = "org_EXAMPLE"
OTHER_ORG = "org_EXAMPLE_OTHER"
PROJECT = "proj_EXAMPLE"
DIMENSION = "device_category"
IDENTITY = "owner@example.com"


class _Cursor:
    """Answers the ONE lookup the guard makes: which org holds this project."""

    def __init__(self, org_of_project):
        self._org_of_project = org_of_project
        self._row = None

    def execute(self, _sql, params=None):
        project_id = (params or (None,))[0]
        org_id = self._org_of_project.get(project_id)
        self._row = (org_id,) if org_id else None

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _Connection:
    def __init__(self, org_of_project):
        self._org_of_project = org_of_project

    def cursor(self):
        return _Cursor(self._org_of_project)


@contextlib.contextmanager
def _connection(org_of_project):
    yield _Connection(org_of_project)


def _door(*, projects=None, member_of=(), manages=()):
    """Patch the guard's two seams: the store, and the two membership questions."""
    projects = projects if projects is not None else {PROJECT: ORG}
    return (
        patch("core.db.get_connection", lambda: _connection(projects)),
        patch(
            "core.project_access.identity_has_org_access",
            lambda org_id, identity, conn: org_id in member_of,
        ),
        patch(
            "core.project_access.identity_can_manage_org",
            lambda org_id, identity, conn: org_id in manages,
        ),
    )


@contextlib.contextmanager
def _client(**kwargs):
    patches = _door(**kwargs)
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        stack.enter_context(
            patch(
                "core.dimension_lineage_api._check_auth",
                new=AsyncMock(return_value=(True, IDENTITY)),
            )
        )
        with TestClient(Router(routes=DIMENSION_LINEAGE_ROUTES)) as c:
            yield c


def _envelope(response) -> tuple[int, dict]:
    return response.status_code, response.json()


def _fed_by(client, project_id=PROJECT, org_id=None):
    params = {"project_id": project_id, "canonical_dimension": DIMENSION}
    if org_id:
        params["org_id"] = org_id
    return client.get("/api/dimension-lineage/fed-by", params=params)


# ---------------------------------------------------------------------------
# THE THREE ENVELOPES ARE ONE.
# ---------------------------------------------------------------------------


def test_an_absent_project_and_a_foreign_one_are_the_same_refusal():
    """The defect, stated as a comparison: two refusals must not differ."""
    with _client(projects={PROJECT: OTHER_ORG}, member_of=()) as client:
        foreign = _envelope(_fed_by(client))
        absent = _envelope(_fed_by(client, project_id="proj_EXAMPLE_ABSENT"))
    assert foreign == absent, (
        "a project you may not see is distinguishable from one that does not "
        f"exist: {foreign} vs {absent}"
    )
    assert foreign[0] == 404


def test_a_project_of_another_organization_than_the_one_named_is_the_same_refusal():
    """Naming org A while pointing at a project of org B teaches nothing either."""
    with _client(projects={PROJECT: OTHER_ORG}, member_of=(ORG,)) as client:
        mismatched = _envelope(_fed_by(client, org_id=ORG))
        absent = _envelope(_fed_by(client, project_id="proj_EXAMPLE_ABSENT"))
    assert mismatched == absent, f"{mismatched} vs {absent}"


def test_a_seam_that_cannot_be_reached_refuses_in_the_same_words():
    """An authorization that cannot be EVALUATED is not an authorization granted,
    and saying so in a 500 is a third envelope to compare against."""
    def _explode():
        raise RuntimeError("store unavailable")

    with _client(member_of=(ORG,)) as client:
        served = _envelope(_fed_by(client))
        with patch("core.db.get_connection", _explode):
            unavailable = _envelope(_fed_by(client))
    assert served[0] == 200
    with _client(projects={}, member_of=()) as client:
        absent = _envelope(_fed_by(client, project_id="proj_EXAMPLE_ABSENT"))
    assert unavailable == absent, f"{unavailable} vs {absent}"


def test_the_two_doors_refuse_in_ONE_vocabulary():
    """The REST door and the model door answer one state; two spellings of one
    refusal are two answers to "who may name here"."""
    from core.mcp_scope import ORG_NOT_FOUND_CODE, ORG_NOT_FOUND_MESSAGE

    with _client(projects={}, member_of=()) as client:
        status, body = _envelope(_fed_by(client, project_id="proj_EXAMPLE_ABSENT"))
    assert status == 404
    assert body["code"] == ORG_NOT_FOUND_CODE
    assert body["message"] == ORG_NOT_FOUND_MESSAGE


# ---------------------------------------------------------------------------
# THE ONE REFUSAL THAT MAY KEEP ITS OWN ENVELOPE.
# ---------------------------------------------------------------------------


def _write(client, **overrides):
    body = {
        "scope_level": "PROJECT",
        "project_id": PROJECT,
        "canonical_dimension": DIMENSION,
        "display_label": "Terminal",
    }
    body.update(overrides)
    return client.post("/api/dimension-lineage/labels", content=json.dumps(body))


def test_a_member_who_is_not_an_admin_is_told_the_gesture_not_the_cause():
    with _client(member_of=(ORG,), manages=()) as client:
        status, body = _envelope(_write(client))
    assert status == 403, body
    assert body["code"] == "forbidden"
    # The gesture a person can perform, in words that are not the store's.
    assert "admin" in body["message"].lower()
    assert "owner" in body["message"].lower()
    assert "sql" not in body["message"].lower()


def test_a_non_member_writing_gets_the_existence_refusal_not_the_403():
    """The 403 above is reachable only by a PROVEN MEMBER. A stranger who could
    read it would learn the organization exists."""
    with _client(projects={PROJECT: OTHER_ORG}, member_of=(), manages=()) as client:
        refused = _envelope(_write(client))
        absent = _envelope(_write(client, project_id="proj_EXAMPLE_ABSENT"))
    assert refused[0] == 404, refused
    assert refused == absent


def test_an_owner_reaches_the_store():
    """The guard is a gate, not a wall: the write travels when the rank is held."""
    seen: dict = {}

    def _set_label(**kwargs):
        seen.update(kwargs)
        return {"id": "dlb_EXAMPLE", **kwargs}

    with _client(member_of=(ORG,), manages=(ORG,)) as client:
        with patch("core.dimension_conformance.set_dimension_label", _set_label):
            status, body = _envelope(_write(client))
    assert status == 201, body
    assert seen["canonical_dimension"] == DIMENSION
    assert seen["project_id"] == PROJECT
    assert seen["identity"] == IDENTITY


# ---------------------------------------------------------------------------
# THE OTHER TWO DOORS OF THE SAME MODULE HOLD THE SAME GUARD.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda c: c.get(
                "/api/dimension-lineage/labels", params={"project_id": PROJECT}
            ),
            id="get-labels",
        ),
        pytest.param(
            lambda c: c.delete(
                f"/api/dimension-lineage/labels/{DIMENSION}",
                params={"scope_level": "PROJECT", "project_id": PROJECT},
            ),
            id="delete-label",
        ),
        pytest.param(lambda c: _write(c), id="post-label"),
    ],
)
def test_every_route_hides_existence_the_same_way(call):
    with _client(projects={PROJECT: OTHER_ORG}, member_of=(), manages=()) as client:
        foreign = _envelope(call(client))
    with _client(projects={}, member_of=(), manages=()) as client:
        absent = _envelope(call(client))
    assert foreign == absent, f"{foreign} vs {absent}"
    assert foreign[0] == 404


def test_the_platform_scope_is_refused_before_any_guard_runs():
    """Nobody writes the platform scope through a door; the refusal names where
    the gesture belongs instead of pretending the address does not exist."""
    with _client(member_of=(ORG,), manages=(ORG,)) as client:
        status, body = _envelope(_write(client, scope_level="PLATFORM"))
    assert status == 403
    assert body["code"] == "forbidden"
    assert "PLATFORM" in body["message"]
