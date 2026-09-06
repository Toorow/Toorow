"""The door of the common key -- the answers a screen has to tell apart (story 66.1).

Proven WITHOUT a database, because what is proven here is the posture, not the
store: the store is proven on real Postgres in
`server/tests/integration/test_mdm_common_keys_pg.py`.

Five answers, and confusing any two of them is a defect this repository has
already shipped once:

  * **401** with no identity -- no answer at all, not an empty one;
  * **404** on a Project the guarded org does not own, never 403: a 403 confirms
    the Project exists (lesson F-3 of story 27.2);
  * **200 + `empty_reason`** when nothing is declared yet. This is the state every
    Project is in on the day this story lands, so a 404 would tell a person the
    page does not exist;
  * **422 + the exact refusal code** when a component is wrong -- the screen puts
    `component_is_metric` beside the component that caused it;
  * **503 with NO list** when the store cannot be read. "I could not look" is not
    "there is nothing".

And 409, which is not a refusal of the request but of the world: a key pinned by
a relationship does not archive.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

IDENTITY = "owner@example.com"
ORG = "org_EXAMPLE"
PROJECT = "proj_EXAMPLE"
BASE = f"/api/projects/{PROJECT}/mdm/common-keys"
KEY = "mck_0000000000000000000000000A"


class _Cursor:
    """The guard no longer reads the Project itself, so this answers nothing.

    It used to assert `"app.projects" in sql` and hand back the org row: the
    guard ran a raw `SELECT org_id FROM app.projects` and asked
    `identity_has_org_access`. It now opens `analyze_connection` -- which arms the
    Epic-36 floor, and therefore issues `SELECT current_user` and `set_config`
    before anything of ours -- and asks `resolve_strict_resource_access`. The
    assertion fired on the FIRST of those statements, the guard's `except`
    turned it into a 500, and fifteen tests read as "the door is broken" when
    what had moved was the seam beneath it.

    A cursor that asserts the SHAPE of a statement it does not own is a test
    pinned to somebody else's implementation. This one accepts what it is given
    and answers nothing, because the decision is now stubbed at its own seam.
    """

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        return None

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _Connection:
    def cursor(self):
        return _Cursor()

    def commit(self):
        return None

    def rollback(self):
        return None


def _serving(org_row=(ORG,), allowed=True):
    """The two seams the guard actually uses today.

    `org_row` is kept in the signature -- several tests pass `org_row=None` to
    mean "this Project is not the guarded org's" -- and is translated into the
    decision the guard now reads, so those call sites keep their meaning.
    """
    from core.project_access import AccessDecision

    @contextmanager
    def _fake_connection(_identity=None):
        yield _Connection()

    decision = (
        AccessDecision(True, "ok", capability="manage", org_id=ORG)
        if allowed and org_row
        else AccessDecision(False, "not_found")
    )
    return (
        patch("core.query_specs_api.analyze_connection", _fake_connection),
        patch("core.project_access.resolve_strict_resource_access", return_value=decision),
    )


def _client():
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    return TestClient(build_asgi_app(), raise_server_exceptions=False)


def _auth_ok():
    return patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, IDENTITY)))


@contextmanager
def _domain(**patches):
    """Patch the domain functions this route delegates to, by name."""
    active = []
    try:
        for name, replacement in patches.items():
            patcher = patch(f"core.mdm_common_keys.{name}", replacement)
            patcher.start()
            active.append(patcher)
        yield
    finally:
        for patcher in reversed(active):
            patcher.stop()


# ===========================================================================
# The addresses exist, in the order the router resolves
# ===========================================================================


def test_the_five_routes_are_mounted_under_the_project_address():
    from core.admin_api import router

    paths = [getattr(route, "path", "") for route in router.routes]
    assert BASE.replace(PROJECT, "{project_id}") in paths
    assert f"{BASE.replace(PROJECT, '{project_id}')}/{{common_key_id}}" in paths
    assert f"{BASE.replace(PROJECT, '{project_id}')}/{{common_key_id}}/versions" in paths


def test_the_versions_segment_is_declared_before_the_parameterised_read():
    """Starlette resolves in order; the order is part of the contract."""
    from core.admin_api import router

    paths = [getattr(route, "path", "") for route in router.routes]
    base = BASE.replace(PROJECT, "{project_id}")
    assert paths.index(f"{base}/{{common_key_id}}/versions") < paths.index(
        f"{base}/{{common_key_id}}"
    )


def test_no_authentication_is_no_answer():
    with patch("core.admin_api._check_auth", new=AsyncMock(return_value=(False, ""))):
        with _client() as client:
            assert client.get(BASE).status_code == 401
            assert client.post(BASE, json={}).status_code == 401


def test_a_project_the_org_does_not_own_is_404_and_never_403():
    connection, access = _serving(allowed=False)
    with _auth_ok(), connection, access, _client() as client:
        response = client.get(BASE)
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


# ===========================================================================
# The empty state, which is what every Project has on the day this lands
# ===========================================================================


def test_no_declared_key_is_200_with_a_reason_and_never_a_404():
    connection, access = _serving()
    with _auth_ok(), connection, access, _domain(list_common_keys=lambda *a, **k: []):
        with _client() as client:
            response = client.get(BASE)
    assert response.status_code == 200
    body = response.json()
    assert body["common_keys"] == []
    assert body["empty_reason"]["code"] == "no_common_key_declared"


def test_the_empty_reason_names_the_gesture_and_no_table():
    connection, access = _serving()
    with _auth_ok(), connection, access, _domain(list_common_keys=lambda *a, **k: []):
        with _client() as client:
            message = client.get(BASE).json()["empty_reason"]["message"]
    for forbidden in ("mdm_common_key", "project_id", "NULL", "row", "migration", "table"):
        assert forbidden not in message, message
    assert "declared" in message


def test_a_list_that_could_not_be_read_is_503_and_carries_no_list():
    def _boom(*_a, **_k):
        raise RuntimeError("the store did not answer")

    connection, access = _serving()
    with _auth_ok(), connection, access, _domain(list_common_keys=_boom):
        with _client() as client:
            response = client.get(BASE)
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "common_keys_unavailable"
    assert "common_keys" not in body


# ===========================================================================
# Refusals reach the screen with the code that says which component was wrong
# ===========================================================================


@pytest.mark.parametrize(
    "code",
    [
        "components_required",
        "component_is_metric",
        "component_not_keyable:money",
        "component_duplicated",
        "component_not_found",
        "common_key_name_taken",
    ],
)
def test_a_refused_declaration_is_422_carrying_its_exact_code(code):
    from core.mdm_common_keys import CommonKeyRefused

    def _refuse(*_a, **_k):
        raise CommonKeyRefused(code, "Refused for a reason a person can repair.")

    connection, access = _serving()
    with _auth_ok(), connection, access, _domain(create_common_key=_refuse):
        with _client() as client:
            response = client.post(BASE, json={"name": "Day", "components": ["mdm_x"]})
    assert response.status_code == 422
    assert response.json()["code"] == code


def test_a_created_key_answers_201_with_its_version():
    def _create(*_a, **_k):
        return {"id": KEY, "current_version": {"id": "mckv_A", "version_number": 1}}

    connection, access = _serving()
    with _auth_ok(), connection, access, _domain(create_common_key=_create):
        with _client() as client:
            response = client.post(BASE, json={"name": "Day", "components": ["mdm_x"]})
    assert response.status_code == 201
    assert response.json()["common_key"]["current_version"]["version_number"] == 1


def test_appending_an_unchanged_version_is_422_and_says_so():
    from core.mdm_common_keys import CommonKeyRefused

    def _refuse(*_a, **_k):
        raise CommonKeyRefused("common_key_unchanged", "Nothing would change.")

    connection, access = _serving()
    with _auth_ok(), connection, access, _domain(append_version=_refuse):
        with _client() as client:
            response = client.post(f"{BASE}/{KEY}/versions", json={"components": ["mdm_x"]})
    assert response.status_code == 422
    assert response.json()["code"] == "common_key_unchanged"


def test_a_key_that_does_not_exist_is_404_not_500():
    from core.mdm_common_keys import CommonKeyNotFound

    def _missing(*_a, **_k):
        raise CommonKeyNotFound(KEY)

    connection, access = _serving()
    with _auth_ok(), connection, access, _domain(read_common_key=_missing):
        with _client() as client:
            response = client.get(f"{BASE}/{KEY}")
    assert response.status_code == 404


# ===========================================================================
# Archiving: a pinned key is a CONFLICT, not a malformed request
# ===========================================================================


def test_archiving_a_pinned_key_is_409_and_names_the_relationships():
    from core.mdm_common_keys import CommonKeyRefused

    def _refuse(*_a, **_k):
        raise CommonKeyRefused(
            "common_key_in_use", "'Day' is pinned by 2 Semantic View relationships."
        )

    connection, access = _serving()
    with _auth_ok(), connection, access, _domain(archive_common_key=_refuse):
        with _client() as client:
            response = client.delete(f"{BASE}/{KEY}")
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "common_key_in_use"
    assert "2 Semantic View relationships" in body["message"]


def test_archiving_an_unpinned_key_answers_200():
    connection, access = _serving()
    with _auth_ok(), connection, access, _domain(
        archive_common_key=lambda *a, **k: {"id": KEY, "status": "archived"}
    ):
        with _client() as client:
            response = client.delete(f"{BASE}/{KEY}")
    assert response.status_code == 200
    assert response.json()["common_key"]["status"] == "archived"
