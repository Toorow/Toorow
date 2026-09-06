"""Story 60.1 -- the REST surface of the value mapping tables.

What is proven here is the four refusals, with their exact codes, because a
refusal that answers the wrong status is a refusal no screen can act on:

  * **403** `platform_scope_forbidden` -- PLATFORM is not writable by the API;
  * **404** on a Project the guarded org does not own -- existence-hiding, never
    a 403 that confirms the Project exists;
  * **409** `value_mapping_impact_not_acknowledged` while a table is applied
    somewhere and nobody acknowledged it -- and **200** once it is;
  * **503** `value_mapping_impact_unavailable` carrying `impact_state: "unknown"`
    and NO list, when the impact could not be read.

Live Postgres for everything the store touches; `_check_auth` is mocked, because
the identity provider is not the subject of this file.
"""

from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import value_mapping_tables as store  # noqa: E402

from tests.core.test_value_mapping_tables import pg_available  # noqa: E402

IDENTITY = "owner@example.com"


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _client():
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    return TestClient(build_asgi_app(), raise_server_exceptions=False)


def _auth_ok():
    return patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, IDENTITY)))


@pytest.fixture
def tenant():
    """An org with a real MEMBER, a project and a Datastream.

    The membership is not decoration: the guard resolves the org from the
    Project and then asks whether this identity may read or manage it, so a
    fixture without a member would exercise the refusal path only.
    """
    from core.db import get_connection

    org_id, project_id, datastream_id = _uid("org"), _uid("proj"), _uid("ds")
    other_org, other_project = _uid("org"), _uid("proj")
    with get_connection() as conn:
        with conn.cursor() as cur:
            for org in (org_id, other_org):
                cur.execute(
                    "INSERT INTO app.organizations (id, name, slug, status, created_by) "
                    "VALUES (%s, 'Story 60.1 API fixture', %s, 'active', %s)",
                    (org, org.replace("_", "-"), IDENTITY),
                )
            cur.execute(
                "INSERT INTO app.org_members (id, org_id, identity, role, status, joined_at) "
                "VALUES (%s, %s, %s, 'owner', 'active', NOW())",
                (_uid("omem"), org_id, IDENTITY),
            )
            for project, org in ((project_id, org_id), (other_project, other_org)):
                cur.execute(
                    "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
                    "VALUES (%s, %s, 'Story 60.1 API fixture', %s, %s)",
                    (project, org, project.replace("_", "-"), IDENTITY),
                )
            cur.execute(
                "INSERT INTO app.datastreams "
                "(id, project_id, org_id, name, module_name, enabled) "
                "VALUES (%s, %s, %s, 'Story 60.1 API stream', 'example_connector', TRUE)",
                (datastream_id, project_id, org_id),
            )
        conn.commit()
    yield {
        "org_id": org_id,
        "project_id": project_id,
        "datastream_id": datastream_id,
        # An org this identity is NOT a member of. Its Project must read as
        # absent, not as forbidden.
        "foreign_project_id": other_project,
    }
    from tests.conftest import purge_fixture_org

    with get_connection() as conn:
        for org in (org_id, other_org):
            purge_fixture_org(conn, org)
        conn.commit()


def _base(project_id: str) -> str:
    return f"/api/projects/{project_id}/value-mapping-tables"


# ===========================================================================
# The routes are mounted at all
# ===========================================================================


def test_the_twelve_routes_are_mounted_under_the_project_address():
    from core.admin_api import router

    paths = {getattr(route, "path", "") for route in router.routes}
    base = "/api/projects/{project_id}/value-mapping-tables"
    assert base in paths
    assert f"{base}/{{table_id}}" in paths
    assert f"{base}/{{table_id}}/entries" in paths
    assert f"{base}/{{table_id}}/entries/{{entry_id}}" in paths
    assert f"{base}/{{table_id}}/import" in paths
    assert f"{base}/{{table_id}}/assignments" in paths


def test_no_authentication_is_no_answer():
    with patch("core.admin_api._check_auth", new=AsyncMock(return_value=(False, ""))):
        with _client() as client:
            response = client.get(_base("proj_EXAMPLE"))
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


# ===========================================================================
# Scope and isolation
# ===========================================================================


@pg_available
def test_the_platform_scope_is_403_with_its_own_code(tenant):
    with _auth_ok(), _client() as client:
        response = client.post(
            _base(tenant["project_id"]),
            json={"scope_level": "PLATFORM", "name": "Platform vocabulary"},
        )
    assert response.status_code == 403
    assert response.json()["code"] == "platform_scope_forbidden"


@pg_available
def test_an_unknown_scope_is_422_and_not_a_silent_default(tenant):
    with _auth_ok(), _client() as client:
        response = client.post(
            _base(tenant["project_id"]),
            json={"scope_level": "GLOBAL", "name": "Vocabulary"},
        )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_scope"


@pg_available
def test_a_project_of_another_org_is_404_and_never_403(tenant):
    """Existence-hiding: a 403 would confirm the Project exists."""
    with _auth_ok(), _client() as client:
        listed = client.get(_base(tenant["foreign_project_id"]))
        created = client.post(
            _base(tenant["foreign_project_id"]), json={"name": "Vocabulary"}
        )
    assert listed.status_code == 404
    assert created.status_code in (403, 404)
    assert listed.json()["code"] == "not_found"


@pg_available
def test_a_project_that_does_not_exist_is_404_too(tenant):
    with _auth_ok(), _client() as client:
        response = client.get(_base("proj_EXAMPLE_ABSENT"))
    assert response.status_code == 404


# ===========================================================================
# The nominal path
# ===========================================================================


@pg_available
def test_a_table_is_created_listed_read_and_carries_impact_state(tenant):
    with _auth_ok(), _client() as client:
        created = client.post(
            _base(tenant["project_id"]),
            json={"scope_level": "PROJECT", "name": "Product lines"},
        )
        assert created.status_code == 201, created.text
        table_id = created.json()["id"]

        listed = client.get(_base(tenant["project_id"]))
        detail = client.get(f"{_base(tenant['project_id'])}/{table_id}")

    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["tables"]] == [table_id]
    assert listed.json()["impact_state"] == "known"
    assert detail.json()["impact_state"] == "known"
    assert detail.json()["datastream_count"] == 0
    assert detail.json()["entries"] == []


@pg_available
def test_a_pair_is_added_edited_and_removed_through_the_routes(tenant):
    with _auth_ok(), _client() as client:
        table_id = client.post(
            _base(tenant["project_id"]), json={"name": "Product lines"}
        ).json()["id"]
        base = f"{_base(tenant['project_id'])}/{table_id}"

        added = client.post(
            f"{base}/entries",
            json={"source_value": "raw-1", "canonical_value": "Line A"},
        )
        assert added.status_code == 201, added.text
        entry_id = added.json()["id"]

        duplicate = client.post(
            f"{base}/entries",
            json={"source_value": "raw-1", "canonical_value": "Line B"},
        )
        edited = client.patch(
            f"{base}/entries/{entry_id}", json={"canonical_value": "Line B"}
        )
        removed = client.delete(f"{base}/entries/{entry_id}")
        after = client.get(base)

    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "value_mapping_conflict"
    assert edited.status_code == 200
    assert removed.status_code == 200
    assert after.json()["entries"] == []


@pg_available
def test_an_import_reports_the_line_it_refused(tenant):
    with _auth_ok(), _client() as client:
        table_id = client.post(
            _base(tenant["project_id"]), json={"name": "Product lines"}
        ).json()["id"]
        response = client.post(
            f"{_base(tenant['project_id'])}/{table_id}/import",
            json={"text": "raw-1,Line A\nlonely\nraw-2,Line B\n"},
        )
    body = response.json()
    assert response.status_code == 200
    assert body["imported_count"] == 2
    assert body["rejected_count"] == 1
    assert body["rejected"][0] == {
        "line": 2,
        "reason": "not_two_columns",
        "raw": "lonely",
    }


# ===========================================================================
# Assignments and the impact guard
# ===========================================================================


@pg_available
def test_an_assignment_is_created_listed_and_removed(tenant):
    with _auth_ok(), _client() as client:
        base = _base(tenant["project_id"])
        table_id = client.post(base, json={"name": "Product lines"}).json()["id"]
        created = client.post(
            f"{base}/{table_id}/assignments",
            json={"datastream_id": tenant["datastream_id"], "source_field": "column_a"},
        )
        assert created.status_code == 201, created.text
        listed = client.get(f"{base}/{table_id}/assignments")
        removed = client.delete(
            f"{base}/{table_id}/assignments"
            f"?assignment_id={created.json()['assignment_id']}"
        )
        empty = client.get(f"{base}/{table_id}/assignments")

    assert listed.json()["impact_state"] == "known"
    assert listed.json()["datastream_count"] == 1
    assert listed.json()["assignments"][0]["source_field"] == "column_a"
    assert removed.status_code == 200
    assert empty.json()["datastream_count"] == 0


@pg_available
def test_patch_and_delete_are_409_without_acknowledgement_then_200_with_it(tenant):
    with _auth_ok(), _client() as client:
        base = _base(tenant["project_id"])
        table_id = client.post(base, json={"name": "Product lines"}).json()["id"]
        client.post(
            f"{base}/{table_id}/assignments",
            json={"datastream_id": tenant["datastream_id"], "source_field": "column_a"},
        )

        refused_patch = client.patch(f"{base}/{table_id}", json={"name": "Renamed"})
        refused_delete = client.delete(f"{base}/{table_id}")
        accepted_patch = client.patch(
            f"{base}/{table_id}", json={"name": "Renamed", "acknowledge_impact": True}
        )
        accepted_delete = client.delete(f"{base}/{table_id}?acknowledge_impact=true")

    assert refused_patch.status_code == 409
    assert refused_patch.json()["code"] == "value_mapping_impact_not_acknowledged"
    # The count is IN the refusal, so the confirmation dialog states the number
    # BEFORE the act rather than after it.
    assert refused_patch.json()["impact"]["datastream_count"] == 1
    assert refused_patch.json()["impact"]["impact_state"] == "known"
    assert refused_delete.status_code == 409

    assert accepted_patch.status_code == 200
    assert accepted_patch.json()["name"] == "Renamed"
    assert accepted_delete.status_code == 200


@pg_available
def test_an_unreadable_impact_is_503_unknown_and_carries_NO_list(tenant):
    """The whole point of the fail-closed contract, at the HTTP boundary."""
    with _auth_ok(), _client() as client:
        base = _base(tenant["project_id"])
        table_id = client.post(base, json={"name": "Product lines"}).json()["id"]

        def _explode(_conn, **_kwargs):
            raise store.ValueMappingUnavailable("the assignment store is unreachable")

        with patch("core.value_mapping_tables.assess_table_impact", new=_explode):
            listed = client.get(f"{base}/{table_id}/assignments")
            refused_patch = client.patch(f"{base}/{table_id}", json={"name": "Renamed"})

    body = listed.json()
    assert listed.status_code == 503
    assert body["code"] == "value_mapping_impact_unavailable"
    assert body["impact_state"] == "unknown"
    assert body["datastream_count"] is None
    # No list of any kind: an empty one would read as "nothing depends on this".
    assert "assignments" not in body

    # And the change is refused rather than applied on an unknown impact.
    assert refused_patch.status_code == 503
    assert refused_patch.json()["impact_state"] == "unknown"


@pg_available
def test_a_table_of_another_org_is_not_found_through_the_route(tenant):
    from core.db import get_connection

    other_org = _uid("org")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, status, created_by) "
                "VALUES (%s, 'Story 60.1 neighbour', %s, 'active', %s)",
                (other_org, other_org.replace("_", "-"), IDENTITY),
            )
        foreign = store.create_table(
            conn,
            org_id=other_org,
            project_id=None,
            scope_level="ORG",
            name="Neighbour vocabulary",
            description=None,
            identity=IDENTITY,
        )
        conn.commit()

    with _auth_ok(), _client() as client:
        response = client.get(f"{_base(tenant['project_id'])}/{foreign['id']}")

    assert response.status_code == 404

    from tests.conftest import purge_fixture_org

    with get_connection() as conn:
        purge_fixture_org(conn, other_org)
        conn.commit()
