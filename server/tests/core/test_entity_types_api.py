"""The door of the declared entity type -- the answers a screen has to tell apart.

Story 68.1, AI-56: proven at the `build_asgi_app()` seam, WITHOUT a database,
because what is proven here is the posture, not the store. The store is proven
on real Postgres in `tests/integration/test_entity_types_pg.py`.

Answers that are not the same, and confusing any two of them is a defect this
repository has already shipped once:

  * **401** with no identity -- no answer at all;
  * **404** on a Project the guarded org does not own, never 403;
  * **200 + `empty_reason`** when nothing is declared yet -- the state every
    Project is in on the day this story lands;
  * **201** a fresh declaration, **200 + `replayed`** the SAME one sent again --
    a replay and a duplicate must be distinguishable;
  * **409 `entity_type_exists`** a DIFFERENT declaration of the same kind -- a
    conflict with the world, naming the kind and its holder;
  * **422 + the exact refusal code** a malformed declaration;
  * **503 with NO list** when the store cannot be read.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

IDENTITY = "owner@example.com"
ORG = "org_EXAMPLE"
PROJECT = "proj_EXAMPLE"
BASE = f"/api/projects/{PROJECT}/master-data/entity-types"

DECLARED = {
    "registry": {
        "id": "mdreg_0000000000000000000000000A",
        "org_id": ORG,
        "project_id": PROJECT,
        "object_kind": "video",
        "label": "Videos",
        "canonical_key": "video_id",
        "lifecycle_state": "draft",
        "version_scope": "node",
        "created_by": IDENTITY,
        "created_at": None,
        "updated_at": None,
    },
    "replayed": False,
}


class _Cursor:
    """Answers nothing: the decision is stubbed at its own seam, not here."""

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


def _serving(allowed=True):
    """The two seams the guard actually uses."""
    from core.project_access import AccessDecision

    @contextmanager
    def _fake_connection(_identity=None):
        yield _Connection()

    decision = (
        AccessDecision(True, "ok", capability="manage", org_id=ORG)
        if allowed
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
            patcher = patch(f"core.object_kind_registry.{name}", replacement)
            patcher.start()
            active.append(patcher)
        yield
    finally:
        for patcher in reversed(active):
            patcher.stop()


# ===========================================================================
# The addresses exist
# ===========================================================================


def test_the_two_routes_are_mounted_under_the_project_address():
    from core.admin_api import router

    by_path = {}
    for route in router.routes:
        by_path.setdefault(getattr(route, "path", ""), set()).update(
            getattr(route, "methods", set())
        )
    base = BASE.replace(PROJECT, "{project_id}")
    assert "GET" in by_path.get(base, set())
    assert "POST" in by_path.get(base, set())


def test_no_authentication_is_no_answer():
    with patch("core.admin_api._check_auth", new=AsyncMock(return_value=(False, ""))):
        with _client() as client:
            assert client.get(BASE).status_code == 401
            assert client.post(BASE, json={}).status_code == 401


def test_a_project_the_org_does_not_own_is_404_and_never_403():
    connection, access = _serving(allowed=False)
    with _auth_ok(), connection, access, _client() as client:
        get_response = client.get(BASE)
        post_response = client.post(
            BASE,
            json={"object_kind": "video", "canonical_key": "video_id", "display_name": "V"},
        )
    assert get_response.status_code == 404
    assert get_response.json()["code"] == "not_found"
    assert post_response.status_code == 404


# ===========================================================================
# The empty state, which is what every Project has on the day this lands
# ===========================================================================


def test_no_declared_type_is_200_with_a_reason_and_never_a_404():
    connection, access = _serving()
    with _auth_ok(), connection, access, _domain(list_entity_types=lambda *a, **k: []):
        with _client() as client:
            response = client.get(BASE)
    assert response.status_code == 200
    body = response.json()
    assert body["entity_types"] == []
    assert body["empty_reason"]["code"] == "no_entity_type_declared"


def test_the_empty_reason_names_the_gesture_and_no_table():
    connection, access = _serving()
    with _auth_ok(), connection, access, _domain(list_entity_types=lambda *a, **k: []):
        with _client() as client:
            message = client.get(BASE).json()["empty_reason"]["message"]
    for forbidden in ("master_data", "registry", "project_id", "NULL", "row", "table"):
        assert forbidden not in message, message
    assert "declared" in message


def test_a_list_carries_the_feeding_state_of_each_type():
    connection, access = _serving()
    rows = [
        {
            "registry_id": "mdreg_0000000000000000000000000A",
            "object_kind": "video",
            "canonical_key": "video_id",
            "display_name": "Videos",
            "lifecycle_state": "draft",
            "version_scope": "node",
            "live_source_count": 0,
            "node_count": 0,
        }
    ]
    with _auth_ok(), connection, access, _domain(list_entity_types=lambda *a, **k: rows):
        with _client() as client:
            body = client.get(BASE).json()
    assert body["entity_types"][0]["live_source_count"] == 0
    assert body["empty_reason"] is None


def test_a_list_that_could_not_be_read_is_503_and_carries_no_list():
    def _boom(*_a, **_k):
        raise RuntimeError("the store did not answer")

    connection, access = _serving()
    with _auth_ok(), connection, access, _domain(list_entity_types=_boom):
        with _client() as client:
            response = client.get(BASE)
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "entity_types_unavailable"
    assert "entity_types" not in body


# ===========================================================================
# Declare: fresh is 201, a replay is 200, a duplicate is 409 with its name
# ===========================================================================


def test_a_fresh_declaration_answers_201_with_the_type():
    connection, access = _serving()
    with _auth_ok(), connection, access, _domain(declare_entity_type=lambda *a, **k: DECLARED):
        with _client() as client:
            response = client.post(
                BASE,
                json={
                    "object_kind": "video",
                    "canonical_key": "video_id",
                    "display_name": "Videos",
                },
            )
    assert response.status_code == 201
    body = response.json()
    assert body["replayed"] is False
    assert body["entity_type"]["canonical_key"] == "video_id"
    assert body["entity_type"]["display_name"] == "Videos"


def test_a_replayed_declaration_answers_200_and_says_so():
    connection, access = _serving()
    replayed = {**DECLARED, "replayed": True}
    with _auth_ok(), connection, access, _domain(declare_entity_type=lambda *a, **k: replayed):
        with _client() as client:
            response = client.post(
                BASE,
                json={
                    "object_kind": "video",
                    "canonical_key": "video_id",
                    "display_name": "Videos",
                },
            )
    assert response.status_code == 200
    assert response.json()["replayed"] is True


def test_a_different_declaration_of_the_same_kind_is_409_naming_the_holder():
    from core.object_kind_registry import EntityTypeExists

    def _refuse(*_a, **_k):
        raise EntityTypeExists(
            "entity type 'video' is already declared in this Project by "
            "registry mdreg_0000000000000000000000000A (declared by owner@example.com) "
            "with canonical key 'video_id' and label 'Videos'"
        )

    connection, access = _serving()
    with _auth_ok(), connection, access, _domain(declare_entity_type=_refuse):
        with _client() as client:
            response = client.post(
                BASE,
                json={
                    "object_kind": "video",
                    "canonical_key": "other_id",
                    "display_name": "Videos",
                },
            )
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "entity_type_exists"
    assert "mdreg_0000000000000000000000000A" in body["message"]


def test_a_malformed_declaration_is_422_carrying_its_code():
    from core.master_data import MasterDataError

    def _refuse(*_a, **_k):
        raise MasterDataError("object_kind must be lowercase snake_case, 2 to 40 characters")

    connection, access = _serving()
    with _auth_ok(), connection, access, _domain(declare_entity_type=_refuse):
        with _client() as client:
            response = client.post(BASE, json={"object_kind": "Video"})
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_master_data_operation"


def test_a_declaration_whose_project_vanished_is_404_not_500():
    from core.master_data import MasterDataNotFound

    def _missing(*_a, **_k):
        raise MasterDataNotFound("registry not found in this Project")

    connection, access = _serving()
    with _auth_ok(), connection, access, _domain(declare_entity_type=_missing):
        with _client() as client:
            response = client.post(BASE, json={"object_kind": "video"})
    assert response.status_code == 404


def test_the_declare_handler_passes_the_caller_identity_as_actor():
    seen = {}

    def _declare(conn, **kwargs):
        seen.update(kwargs)
        return DECLARED

    connection, access = _serving()
    with _auth_ok(), connection, access, _domain(declare_entity_type=_declare):
        with _client() as client:
            client.post(
                BASE,
                json={
                    "object_kind": "video",
                    "canonical_key": "video_id",
                    "display_name": "Videos",
                },
            )
    assert seen["actor"] == IDENTITY
    assert seen["org_id"] == ORG
    assert seen["project_id"] == PROJECT
    assert seen["canonical_key"] == "video_id"
    assert seen["display_name"] == "Videos"
